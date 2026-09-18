import math

from shapely.geometry import Point, mapping
from shapely.ops import unary_union

from correlation_env import drift_vector_ms

DRIFT_MODEL_VERSION = "lagrangian-backtrack-0.1.0"
MAX_HOURS, STEP_H = 72, 1.0
K_DIFF_M2S = 10.0          # horizontal eddy diffusivity (m²/s) → turbulent spread
VEL_UNCERT_FRAC = 0.35     # fractional uncertainty on the drift velocity (forcing error)
MIN_SIGMA_KM = 0.6


def sigma_km(hours: float, speed_ms: float) -> float:
    """1-σ position uncertainty after back-tracking `hours`: forcing error grows linearly, turbulent diffusion as √t."""
    t = hours * 3600
    diffusion = math.sqrt(2 * K_DIFF_M2S * t) / 1000
    forcing = VEL_UNCERT_FRAC * speed_ms * t / 1000
    return max(MIN_SIGMA_KM, math.hypot(diffusion, forcing))


def backtrack(lat0: float, lon0: float, wind, current, hours: float = MAX_HOURS):
    """Hourly reverse Lagrangian steps (3% wind + surface current). Returns list of {h, lat, lon, sigma_km}."""
    vx, vy = drift_vector_ms(wind, current)
    speed = math.hypot(vx, vy)
    path = [{"h": 0.0, "lat": lat0, "lon": lon0, "sigma_km": sigma_km(0, speed)}]
    lat, lon = lat0, lon0
    h = 0.0
    while h < hours - 1e-9:
        h += STEP_H
        dt = STEP_H * 3600
        lat -= (vy * dt / 1000) / 110.574
        lon -= (vx * dt / 1000) / (111.32 * math.cos(math.radians(lat)))
        path.append({"h": round(h, 2), "lat": round(lat, 6), "lon": round(lon, 6), "sigma_km": round(sigma_km(h, speed), 3)})
    return path, speed


def position_at(path, h: float):
    if h <= 0:
        return path[0]
    if h >= path[-1]["h"]:
        return path[-1]
    i = int(h // STEP_H)
    a, b = path[i], path[min(i + 1, len(path) - 1)]
    f = (h - a["h"]) / max(b["h"] - a["h"], 1e-9)
    return {"h": h, "lat": a["lat"] + (b["lat"] - a["lat"]) * f, "lon": a["lon"] + (b["lon"] - a["lon"]) * f, "sigma_km": a["sigma_km"] + (b["sigma_km"] - a["sigma_km"]) * f}


def _buffer_deg(p, k_sigma=2.0):
    r_km = k_sigma * p["sigma_km"]
    ry = r_km / 110.574
    rx = r_km / (111.32 * math.cos(math.radians(p["lat"])))
    circ = Point(p["lon"], p["lat"]).buffer(1.0, resolution=24)
    return __import__("shapely.affinity", fromlist=["scale"]).scale(circ, rx, ry, origin=(p["lon"], p["lat"]))


def envelope(path, up_to_h: float, k_sigma=2.0):
    pts = [p for p in path if p["h"] <= up_to_h + 1e-9]
    return unary_union([_buffer_deg(p, k_sigma) for p in pts])


def build_drift_model(spill_lat, spill_lon, wind, current, window_hours: float, spill_age_hours=None):
    hours = min(MAX_HOURS, max(1.0, window_hours))
    path, speed = backtrack(spill_lat, spill_lon, wind, current, hours)
    env_all = envelope(path, hours)
    likely_lo, likely_hi = (max(0.0, spill_age_hours * 0.5), min(hours, spill_age_hours * 1.5)) if spill_age_hours else (0.0, hours)
    likely = envelope([position_at(path, h) for h in _hour_range(likely_lo, likely_hi)] or [path[0]], likely_hi)
    return {
        "version": DRIFT_MODEL_VERSION, "hours": hours, "step_hours": STEP_H, "drift_speed_ms": round(speed, 3),
        "path": {"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in path]},
        "path_hours": [p["h"] for p in path], "sigma_km": [p["sigma_km"] for p in path],
        "envelope": mapping(env_all), "likely_envelope": mapping(likely), "likely_window_hours": [round(likely_lo, 1), round(likely_hi, 1)],
        "k_sigma": 2.0, "diffusivity_m2s": K_DIFF_M2S, "velocity_uncertainty_frac": VEL_UNCERT_FRAC,
        "note": "Lightweight Lagrangian back-track (3% wind + surface current, hourly steps). Envelope = 2σ origin region growing with time. Full OpenDrift/HYCOM on backlog.",
    }


def _hour_range(lo, hi):
    out, h = [], lo
    while h <= hi + 1e-9:
        out.append(h)
        h += STEP_H
    return out


def score_fixes(path, fixes, t0):
    """For each fix before acquisition within model horizon: z = distance / σ(h). Returns best (score, detail dict)."""
    from geo import haversine_km
    best = None
    for p in fixes:
        h = (t0 - p["timestamp"]).total_seconds() / 3600
        if h < 0 or h > path[-1]["h"]:
            continue
        o = position_at(path, h)
        d = haversine_km(p["lat"], p["lon"], o["lat"], o["lon"])
        z = d / o["sigma_km"]
        s = math.exp(-0.5 * z * z)
        if best is None or s > best["score"]:
            best = {"score": s, "hours_before": round(h, 2), "distance_km": round(d, 3), "sigma_km": round(o["sigma_km"], 3), "z": round(z, 2), "inside_2sigma": z <= 2.0,
                    "fix_id": p["id"], "interpolated": bool(p.get("interpolated")), "origin_estimate": {"type": "Point", "coordinates": [round(o["lon"], 6), round(o["lat"], 6)]}}
    return best
