"""Correlation engine corr-1.1.0 — a six-stage pipeline:
load_inputs → build_corridor → fill_and_filter_tracks → score_candidates → rank_and_status → assemble_result.
Deterministic: identical inputs yield identical output (see tests/test_correlation_golden.py)."""
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import shape

from geo import haversine_km, distance_to_geom_km, major_axis_bearing, max_extent_km, bearing_deg
from models import CorrelationParams
from correlation_env import drift_vector_ms
from gapfill import fill_gaps, GAP_FILL_VERSION
from drift import build_drift_model, score_fixes, position_at, DRIFT_MODEL_VERSION

ALGORITHM_VERSION = "corr-1.1.0"
SEVERE_FLAGS = {"natural_seep_suspect", "low_wind", "sunglint", "conflicting_source", "cloud_contaminated", "lookalike_suspect"}
RELIABILITY_PENALTIES = {
    "spoof_suspect": 0.4, "position_jump": 0.3, "implausible_speed": 0.2, "naive_timestamp": 0.1,
    "stale": 0.2, "missing_identity": 0.1, "future_timestamp": 0.3, "interpolated": 0.0,
}
STATUS_ORDER = ["insufficient_evidence", "possible", "probable"]
AMBIGUITY_DELTA = 0.08


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def cap_status(status: str, cap: str) -> str:
    return STATUS_ORDER[min(STATUS_ORDER.index(status), STATUS_ORDER.index(cap))]


_path_cache: dict = {}


def drift_model_path(model: dict) -> List[dict]:
    """Rebuild the hourly path list from the stored model (cached per model object)."""
    key = id(model)
    if key not in _path_cache:
        coords = model["path"]["coordinates"]
        _path_cache.clear()
        _path_cache[key] = [{"h": h, "lat": c[1], "lon": c[0], "sigma_km": s} for h, c, s in zip(model["path_hours"], coords, model["sigma_km"])]
    return _path_cache[key]


def resolve_environment(spill: dict, params: CorrelationParams) -> Tuple[Optional[dict], Optional[dict]]:
    wind = params.wind.model_dump() if params.wind else (spill.get("wind") if params.use_observation_environment else None)
    current = params.current.model_dump() if params.current else (spill.get("current") if params.use_observation_environment else None)
    return wind, current


def input_hash(spill: dict, positions: List[dict], params: CorrelationParams, wind: Optional[dict], current: Optional[dict]) -> str:
    payload = {
        "algorithm": ALGORITHM_VERSION,
        "spill": {"id": spill["id"], "geometry": spill["geometry"], "t": spill["acquisition_time"].isoformat(),
                  "flags": sorted(spill.get("quality_flags", [])), "conf": spill["detection_confidence"]},
        "params": params.model_dump(), "wind": wind, "current": current,
        "positions": sorted((p["id"], p["timestamp"].isoformat(), p["lat"], p["lon"]) for p in positions),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class ProcessingLog:
    def __init__(self) -> None:
        self.entries: List[dict] = []

    def __call__(self, msg: str, level: str = "info") -> None:
        self.entries.append({"t": datetime.now(timezone.utc).isoformat(), "level": level, "msg": msg})


@dataclass
class Context:
    """Everything the stages share; populated progressively."""
    spill: dict
    positions: List[dict]
    params: CorrelationParams
    log: ProcessingLog = field(default_factory=ProcessingLog)
    poly: Any = None
    centroid: Any = None
    t0: datetime = None
    axis: float = 0.0
    wind: Optional[dict] = None
    current: Optional[dict] = None
    degraded: bool = True
    severe: List[str] = field(default_factory=list)
    low_conf: bool = False
    spill_age: Optional[float] = None
    drift_model: Optional[dict] = None
    by_vessel: Dict[str, List[dict]] = field(default_factory=dict)
    tracks: List[Tuple[str, List[dict], List[dict], List[dict]]] = field(default_factory=list)  # (mmsi, real_fixes, fixes, segments)
    candidates: List[dict] = field(default_factory=list)
    ambiguous: bool = False
    overall: str = "insufficient_evidence"
    band: str = "low"


# ── stage 1 ────────────────────────────────────────────────────────────────
def load_inputs(ctx: Context) -> Context:
    """Geometry, acquisition time, environment and spill-quality caveats."""
    s, p, L = ctx.spill, ctx.params, ctx.log
    ctx.poly = shape(s["geometry"])
    ctx.centroid = ctx.poly.centroid
    ctx.t0 = s["acquisition_time"]
    ctx.axis = major_axis_bearing(ctx.poly)
    ctx.wind, ctx.current = resolve_environment(s, p)
    ctx.degraded = not (ctx.wind or ctx.current)
    ctx.severe = sorted(set(s.get("quality_flags", [])) & SEVERE_FLAGS)
    ctx.low_conf = s["detection_confidence"] < 0.4
    ctx.spill_age = p.spill_age_hours if p.spill_age_hours is not None else s.get("estimated_age_hours")
    L(f"Algorithm {ALGORITHM_VERSION}; spill {s['id'][:8]} centroid ({ctx.centroid.y:.4f}, {ctx.centroid.x:.4f}), "
      f"extent {max_extent_km(ctx.poly):.1f} km, major axis {ctx.axis:.0f}°, detection confidence {s['detection_confidence']:.2f}")
    L(f"Search corridor {p.corridor_km} km; window -{p.window_hours_before}h / +{p.window_hours_after}h")
    return ctx


# ── stage 2 ────────────────────────────────────────────────────────────────
def build_corridor(ctx: Context) -> Context:
    """Backward drift model (or degraded mode) and status-cap warnings."""
    L, p = ctx.log, ctx.params
    if ctx.degraded:
        L("No wind/current inputs available — drift-back uncertainty unresolved; attribution marked DEGRADED", "warn")
        ctx.drift_model = None
    else:
        vx, vy = drift_vector_ms(ctx.wind, ctx.current)
        L(f"Drift model: 3% wind + surface current → {math.hypot(vx, vy):.2f} m/s toward {(math.degrees(math.atan2(vx, vy)) + 360) % 360:.0f}°")
        dm = ctx.drift_model = build_drift_model(ctx.centroid.y, ctx.centroid.x, ctx.wind, ctx.current, p.window_hours_before, ctx.spill_age)
        L(f"Backward Lagrangian model {DRIFT_MODEL_VERSION}: {dm['hours']:.0f} h × {dm['step_hours']:.0f} h steps, 2σ envelope grows to {dm['sigma_km'][-1] * 2:.1f} km; likely origin window {dm['likely_window_hours'][0]}–{dm['likely_window_hours'][1]} h before acquisition")
    if ctx.severe:
        L(f"Spill quality flags {ctx.severe} — candidate statuses capped at 'possible'", "warn")
    if ctx.low_conf:
        L("Detection confidence < 0.4 — statuses capped at 'possible'", "warn")
    if ctx.spill_age is None:
        L("Spill age unknown — time-gap scoring uses full search window", "warn")
    return ctx


# ── stage 3 ────────────────────────────────────────────────────────────────
def fill_and_filter_tracks(ctx: Context) -> Context:
    """Group fixes per vessel, sort deterministically, dead-reckon across AIS gaps."""
    L, p = ctx.log, ctx.params
    for pos in ctx.positions:
        ctx.by_vessel.setdefault(pos["mmsi"], []).append(pos)
    L(f"{len(ctx.positions)} AIS fixes from {len(ctx.by_vessel)} vessels inside corridor/time window")
    if p.fill_gaps:
        L(f"AIS gap filling {GAP_FILL_VERSION}: dead-reckoning across gaps > {p.gap_threshold_min} min; implausible transits flagged spoof_suspect")
    for mmsi, fixes in sorted(ctx.by_vessel.items()):
        fixes.sort(key=lambda x: (x["timestamp"], x["id"]))
        real_fixes = list(fixes)
        segments: List[dict] = []
        if p.fill_gaps:
            fixes, segments = fill_gaps(fixes, p.gap_threshold_min)
        ctx.tracks.append((mmsi, real_fixes, fixes, segments))
    return ctx


# ── stage 4: per-factor scorers ────────────────────────────────────────────
def _closest_approach(ctx: Context, fixes: List[dict]):
    dists = [distance_to_geom_km(ctx.poly, f["lat"], f["lon"]) for f in fixes]
    i_min = min(range(len(fixes)), key=lambda i: (dists[i], i))
    return i_min, fixes[i_min], dists[i_min]


def _spatial(ctx: Context, closest: dict, d_min: float, notes: List[str]) -> Tuple[float, float]:
    spatial = math.exp(-d_min / (ctx.params.corridor_km / 3))
    gap_penalty = 0.0
    if closest.get("interpolated"):
        gap_penalty = clamp(closest["gap_hours"] / 12, 0, 0.6)
        spatial *= 1 - gap_penalty
        notes.append(f"closest approach is an interpolated position inside a {closest['gap_hours']}h AIS gap — spatial score reduced {gap_penalty * 100:.0f}%")
    return spatial, gap_penalty


def _temporal(ctx: Context, gap_h: float, notes: List[str]) -> float:
    p = ctx.params
    if gap_h >= 0:
        temporal = clamp(1 - gap_h / p.window_hours_before)
    else:
        temporal = clamp(1 - abs(gap_h) / max(p.window_hours_after, 0.01)) * 0.3
        notes.append("closest approach occurred after acquisition (weak evidence)")
    if ctx.spill_age is not None and gap_h > ctx.spill_age * 1.5:
        temporal *= 0.5
        notes.append(f"time gap {gap_h:.1f}h exceeds 1.5× estimated spill age {ctx.spill_age}h")
    return temporal


def _continuity(ctx: Context, real_fixes: List[dict], closest: dict, segments: List[dict], notes: List[str]):
    """Returns (continuity, max_gap, jumps) from real (transmitted) fixes only."""
    n, max_gap, jumps, dark_over_spill = len(real_fixes), 0.0, set(), False
    for a, b in zip(real_fixes, real_fixes[1:]):
        dt_h = (b["timestamp"] - a["timestamp"]).total_seconds() / 3600
        max_gap = max(max_gap, dt_h)
        if dt_h > 0 and haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) / 1.852 / dt_h > 50:
            jumps.add("position_jump")
        if dt_h > 2 and a["timestamp"] <= closest["timestamp"] <= b["timestamp"] + (b["timestamp"] - a["timestamp"]):
            dark_over_spill = True
    continuity = 0.0 if n < ctx.params.min_positions else clamp(1 - max(0.0, max_gap - 0.5) / 6)
    if dark_over_spill:
        notes.append(f"AIS gap of {max_gap:.1f}h adjacent to closest approach (possible dark period)")
    spoof_segs = [s for s in segments if s["spoof_suspect"]]
    if spoof_segs:
        jumps.add("spoof_suspect")
        notes.append(f"{len(spoof_segs)} gap(s) require {max(s['required_speed_kn'] for s in spoof_segs):.0f} kn to close — kinematically implausible, flagged spoof_suspect (not interpolated)")
    filled = [s for s in segments if s["interpolated_points"]]
    if filled:
        notes.append(f"{len(filled)} AIS gap(s) totalling {sum(s['gap_hours'] for s in filled):.1f}h filled by dead reckoning ({sum(s['interpolated_points'] for s in filled)} synthetic points, dashed on map)")
    return continuity, max_gap, jumps


def _heading(ctx: Context, fixes: List[dict], i_min: int, closest: dict, n_real: int) -> Tuple[float, str]:
    sog = closest.get("sog_kn") or 0.0
    cog = closest.get("cog_deg")
    if cog is None and i_min + 1 < n_real:
        nxt = fixes[i_min + 1]
        cog = bearing_deg(closest["lat"], closest["lon"], nxt["lat"], nxt["lon"])
    if sog < 0.5 or cog is None:
        return 0.4, "vessel stationary or COG unavailable — neutral"
    diff = abs((cog % 180) - ctx.axis)
    diff = min(diff, 180 - diff)
    return clamp(1 - diff / 90), f"COG {cog:.0f}° vs spill axis {ctx.axis:.0f}° (Δ {diff:.0f}°)"


def _drift(ctx: Context, fixes: List[dict], gap_h: float):
    """Returns (drift, detail, backprojected_point, drift_match)."""
    if gap_h < 0:
        return 0.15, "vessel closest approach post-dates acquisition — cannot precede observed slick", None, None
    if ctx.degraded:
        return 0.5, "no wind/current — neutral score, attribution degraded", None, None
    path = drift_model_path(ctx.drift_model)
    o = position_at(path, gap_h)
    backprojected = {"type": "Point", "coordinates": [round(o["lon"], 6), round(o["lat"], 6)]}
    match = score_fixes(path, fixes, ctx.t0)
    if not match:
        return 0.2, "no fixes before acquisition inside the 72h model horizon", backprojected, None
    drift = match["score"]
    detail = (f"best fix {match['hours_before']}h before acquisition lies {match['distance_km']:.1f} km from back-tracked origin (σ {match['sigma_km']:.1f} km, z={match['z']}) — "
              + ("inside 2σ origin envelope" if match["inside_2sigma"] else "outside 2σ origin envelope") + (" · via interpolated position" if match["interpolated"] else ""))
    if match["interpolated"]:
        drift *= 0.8
    return drift, detail, backprojected, match


def _reliability(real_fixes: List[dict], closest: dict, jumps: set) -> Tuple[float, List[str]]:
    flags = set(jumps)
    for f in real_fixes:
        flags |= set(f.get("quality_flags", []))
    if not closest.get("imo") and not closest.get("vessel_name"):
        flags.add("missing_identity")
    flags = sorted(flags)
    return clamp(1 - sum(RELIABILITY_PENALTIES.get(f, 0.05) for f in flags)), flags


def _weighted(params: CorrelationParams, factors: dict) -> float:
    w = params.weights
    total_w = sum(w.get(k, 0) for k in factors) or 1.0
    score = sum(w.get(k, 0) * factors[k]["score"] for k in factors) / total_w
    for k in factors:
        factors[k]["weight"] = w.get(k, 0)
        factors[k]["contribution"] = round(w.get(k, 0) * factors[k]["score"] / total_w, 4)
    return score


def _initial_status(ctx: Context, score: float, reliability: float, n: int, fixes_before: int, notes: List[str]) -> str:
    status = "probable" if score >= 0.7 else "possible" if score >= 0.45 else "insufficient_evidence"
    if ctx.severe or ctx.low_conf:
        status = cap_status(status, "possible")
        notes.append("status capped: spill observation quality/confidence concerns")
    if reliability < 0.4:
        status = cap_status(status, "possible")
        notes.append("status capped: low AIS reliability")
    if n < ctx.params.min_positions:
        status = "insufficient_evidence"
        notes.append(f"fewer than {ctx.params.min_positions} AIS fixes in window")
    if fixes_before == 0:
        status = "insufficient_evidence"
        notes.append("no AIS fixes before acquisition time")
    return status


def score_vessel(ctx: Context, mmsi: str, real_fixes: List[dict], fixes: List[dict], segments: List[dict]) -> dict:
    """Six explainable factors → weighted score → provisional status for one vessel."""
    notes: List[str] = []
    n = len(real_fixes)
    i_min, closest, d_min = _closest_approach(ctx, fixes)
    gap_h = (ctx.t0 - closest["timestamp"]).total_seconds() / 3600
    spatial, gap_penalty = _spatial(ctx, closest, d_min, notes)
    temporal = _temporal(ctx, gap_h, notes)
    fixes_before = sum(1 for f in fixes if f["timestamp"] <= ctx.t0)
    continuity, max_gap, jumps = _continuity(ctx, real_fixes, closest, segments, notes)
    heading, heading_detail = _heading(ctx, fixes, i_min, closest, n)
    drift, drift_detail, backprojected, drift_match = _drift(ctx, fixes, gap_h)
    reliability, flags = _reliability(real_fixes, closest, jumps)
    factors = {
        "spatial": {"score": round(spatial, 4), "detail": f"closest approach {d_min:.2f} km from spill boundary"},
        "temporal": {"score": round(temporal, 4), "detail": f"{gap_h:.1f}h before acquisition" if gap_h >= 0 else f"{abs(gap_h):.1f}h after acquisition"},
        "continuity": {"score": round(continuity, 4), "detail": f"{n} fixes, max gap {max_gap:.1f}h"},
        "heading": {"score": round(heading, 4), "detail": heading_detail},
        "drift": {"score": round(drift, 4), "detail": drift_detail},
        "reliability": {"score": round(reliability, 4), "detail": f"AIS flags: {', '.join(flags) if flags else 'none'}"},
    }
    score = _weighted(ctx.params, factors)
    status = _initial_status(ctx, score, reliability, n, fixes_before, notes)
    return {
        "mmsi": mmsi, "imo": closest.get("imo"), "vessel_name": closest.get("vessel_name"),
        "vessel_type": closest.get("vessel_type"), "score": round(score, 4), "status": status,
        "factors": factors, "notes": notes, "ais_flags": flags,
        "evidence": {
            "closest_fix": {"id": closest["id"], "timestamp": closest["timestamp"], "lat": closest["lat"], "lon": closest["lon"],
                            "sog_kn": closest.get("sog_kn"), "cog_deg": closest.get("cog_deg"), "source": closest.get("source")},
            "distance_km": round(d_min, 3), "time_gap_hours": round(gap_h, 3), "fix_count": n, "interpolated_count": len(fixes) - n,
            "max_gap_hours": round(max_gap, 2), "backprojected_centroid": backprojected, "drift_match": drift_match, "gap_penalty": round(gap_penalty, 3),
            "gap_segments": segments, "closest_is_interpolated": bool(closest.get("interpolated")),
            "fix_ids": [f["id"] for f in real_fixes],
        },
        "track": [{"timestamp": f["timestamp"], "lat": f["lat"], "lon": f["lon"], "sog_kn": f.get("sog_kn"), "cog_deg": f.get("cog_deg"), "interpolated": bool(f.get("interpolated"))} for f in fixes[:800]],
    }


def score_candidates(ctx: Context) -> Context:
    ctx.candidates = [score_vessel(ctx, *t) for t in ctx.tracks]
    return ctx


# ── stage 5 ────────────────────────────────────────────────────────────────
def rank_and_status(ctx: Context) -> Context:
    """Deterministic ranking, multi-vessel ambiguity cap, overall status and confidence band."""
    L, c = ctx.log, ctx.candidates
    c.sort(key=lambda x: (-STATUS_ORDER.index(x["status"]), -x["score"], x["mmsi"]))
    ctx.ambiguous = len(c) >= 2 and c[0]["score"] - c[1]["score"] < AMBIGUITY_DELTA
    if ctx.ambiguous:
        L(f"Top two candidates within {AMBIGUITY_DELTA} score — multiple-vessel ambiguity, statuses capped at 'possible'", "warn")
        for x in c[:2]:
            x["status"] = cap_status(x["status"], "possible")
            x["notes"].append("status capped: comparable evidence for multiple vessels")
    for i, x in enumerate(c):
        x["rank"] = i + 1
        L(f"#{x['rank']} {x['vessel_name'] or x['mmsi']} (MMSI {x['mmsi']}): score {x['score']:.3f} → {x['status']}")
    if ctx.severe and ctx.spill["detection_confidence"] < 0.5:
        ctx.overall = "indeterminate"
        L("Overall: INDETERMINATE — spill observation ambiguous (possible lookalike/seep) with low confidence", "warn")
    elif not c:
        ctx.overall = "insufficient_evidence"
        L("Overall: no AIS candidates within corridor/time window — insufficient evidence")
    else:
        ctx.overall = c[0]["status"]
        L(f"Overall attribution status: {ctx.overall}")
    ctx.band = "high" if ctx.overall == "probable" and not ctx.degraded else "medium" if ctx.overall in ("probable", "possible") else "low"
    return ctx


# ── stage 6 ────────────────────────────────────────────────────────────────
def assemble_result(ctx: Context) -> dict:
    p = ctx.params
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "params": p.model_dump(),
        "environment": {"wind": ctx.wind, "current": ctx.current, "source": "observation" if (ctx.wind or ctx.current) and not (p.wind or p.current) else "params"},
        "degraded": ctx.degraded,
        "spill_quality_flags": sorted(ctx.spill.get("quality_flags", [])),
        "severe_flags": ctx.severe,
        "ambiguous_multiple_vessels": ctx.ambiguous,
        "spill_axis_bearing": ctx.axis,
        "drift_model": ctx.drift_model,
        "gap_fill": {"enabled": p.fill_gaps, "version": GAP_FILL_VERSION if p.fill_gaps else None, "threshold_min": p.gap_threshold_min},
        "candidates": ctx.candidates,
        "overall_status": ctx.overall,
        "confidence_band": ctx.band,
        "top_score": round(ctx.candidates[0]["score"] if ctx.candidates else 0.0, 4),
        "input_hash": input_hash(ctx.spill, ctx.positions, p, ctx.wind, ctx.current),
        "position_count": len(ctx.positions),
        "vessel_count": len(ctx.by_vessel),
        "processing_log": ctx.log.entries,
    }


PIPELINE = (load_inputs, build_corridor, fill_and_filter_tracks, score_candidates, rank_and_status)


def run_correlation(spill: dict, positions: List[dict], params: CorrelationParams) -> dict:
    ctx = Context(spill=spill, positions=positions, params=params)
    for stage in PIPELINE:
        ctx = stage(ctx)
    return assemble_result(ctx)
