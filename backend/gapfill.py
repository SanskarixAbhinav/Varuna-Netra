from datetime import timedelta
from typing import List, Optional, Tuple

from geo import haversine_km, destination, bearing_deg

GAP_FILL_VERSION = "gapfill-dr-0.1.0"
MAX_SPEED_KN = {"tanker": 22, "cargo": 27, "fishing": 18, "passenger": 38, "tug": 20, "default": 45}
STEP_MIN = 10
CARRY_KEYS = ("mmsi", "imo", "vessel_name", "vessel_type", "source")


def max_speed_for(vessel_type: Optional[str]) -> int:
    vt = (vessel_type or "").lower()
    for k, v in MAX_SPEED_KN.items():
        if k in vt:
            return v
    return MAX_SPEED_KN["default"]


def _dead_reckon(a: dict, b: dict, dt_h: float, d_km: float) -> List[dict]:
    """Synthetic fixes between a and b: dead reckoning from a's SOG/COG, blended linearly onto b."""
    sog = a.get("sog_kn") or (d_km / 1.852 / dt_h)
    cog = a.get("cog_deg") if a.get("cog_deg") is not None else bearing_deg(a["lat"], a["lon"], b["lat"], b["lon"])
    n = max(1, int(dt_h * 60 // STEP_MIN))
    dr_end = destination(a["lat"], a["lon"], cog, sog * 1.852 * dt_h)
    err_lat, err_lon = b["lat"] - dr_end[0], b["lon"] - dr_end[1]
    pts = []
    for i in range(1, n):
        f = i / n
        dr = destination(a["lat"], a["lon"], cog, sog * 1.852 * dt_h * f)
        pts.append({**{k: a.get(k) for k in CARRY_KEYS}, "id": f"{a['id']}~{i}", "timestamp": a["timestamp"] + timedelta(hours=dt_h * f),
                    "lat": round(dr[0] + err_lat * f, 6), "lon": round(dr[1] + err_lon * f, 6), "sog_kn": sog, "cog_deg": cog, "quality_flags": ["interpolated"],
                    "interpolated": True, "gap_hours": round(dt_h, 2), "segment_from": a["id"]})
    return pts


def fill_gaps(fixes: List[dict], threshold_min: float = 30, max_gap_hours: float = 12) -> Tuple[List[dict], List[dict]]:
    """Dead-reckon across AIS gaps > threshold. Synthetic fixes carry interpolated=True; implausible transits are flagged spoof_suspect."""
    if len(fixes) < 2:
        return list(fixes), []
    out, segments = [fixes[0]], []
    for a, b in zip(fixes, fixes[1:]):
        dt_h = (b["timestamp"] - a["timestamp"]).total_seconds() / 3600
        if dt_h * 60 > threshold_min:
            d_km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            need_kn = d_km / 1.852 / max(dt_h, 1e-6)
            vmax = max_speed_for(a.get("vessel_type") or b.get("vessel_type"))
            seg = {"from_fix": a["id"], "to_fix": b["id"], "start": a["timestamp"], "end": b["timestamp"], "gap_hours": round(dt_h, 2), "distance_km": round(d_km, 2),
                   "required_speed_kn": round(need_kn, 1), "vessel_max_kn": vmax, "spoof_suspect": need_kn > vmax, "interpolated_points": 0, "method": "dead_reckoning_blend"}
            if not seg["spoof_suspect"] and dt_h <= max_gap_hours:
                pts = _dead_reckon(a, b, dt_h, d_km)
                out.extend(pts)
                seg["interpolated_points"] = len(pts)
            segments.append(seg)
        out.append(b)
    return out, segments
