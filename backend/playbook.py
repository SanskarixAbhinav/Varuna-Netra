import math
from datetime import timedelta
from typing import Optional, Tuple

import numpy as np
from shapely.geometry import shape

from db import db, to_utc
from geo import destination
from correlation_env import drift_vector_ms

PLAYBOOK_VERSION = "playbook-rules-0.1.0"
THICKNESS_UM = {"sheen": 0.1, "thin": 1.0, "thick": 10.0}  # µm assumptions per appearance class
DISPERSANT_WIND = (4, 12)
_ = np  # numpy must be importable for global_land_mask


def _globe():
    from global_land_mask import globe  # lazy: loads a large mask array
    return globe


def coast_distance_km(lat: float, lon: float, max_km: float = 300) -> Tuple[float, str]:
    """Approximate distance to nearest land using the 1-km global land mask (radial search)."""
    globe = _globe()
    if globe.is_land(lat, lon):
        return 0.0, "on land mask (shoreline/estuary)"
    for r in [1, 2, 5, 10, 15, 20, 30, 40, 50, 75, 100, 150, 200, 300]:
        if r > max_km:
            break
        for b in range(0, 360, 15):
            la, lo = destination(lat, lon, b, r)
            if -90 <= la <= 90 and globe.is_land(la, ((lo + 180) % 360) - 180):
                return float(r), f"nearest land ≈{r} km bearing {b}°"
    return float(max_km), f">{max_km} km offshore"


def _environment(spill: dict, result: Optional[dict]):
    if result:
        env = result.get("environment", {})
        return env.get("wind"), env.get("current")
    return spill.get("wind"), spill.get("current")


def _situation(spill: dict, result: Optional[dict]) -> dict:
    """Derived physical picture: thickness/volume, coast & depth, sea state, drift vector, slick extent."""
    area = spill.get("estimated_area_km2") or 0.0
    lon, lat = spill["centroid"]["coordinates"]
    wind, current = _environment(spill, result)
    thick_class = "thick" if area < 2 else "thin" if area < 25 else "sheen"
    vol_m3 = {k: area * 1e6 * v * 1e-6 for k, v in THICKNESS_UM.items()}
    coast_km, coast_note = coast_distance_km(lat, lon)
    wind_ms = wind["speed_ms"] if wind else None
    vx, vy = drift_vector_ms(wind, current) if (wind or current) else (0.0, 0.0)
    drift_speed = math.hypot(vx, vy)
    b = shape(spill["geometry"]).bounds
    return {
        "area": area, "conf": spill.get("detection_confidence") or 0, "lat": lat, "lon": lon, "thick_class": thick_class,
        "vol_m3": vol_m3, "vol_t": {k: round(v * 0.9, 1) for k, v in vol_m3.items()},
        "coast_km": coast_km, "coast_note": coast_note,
        "depth_class": "shallow (<50 m likely)" if coast_km < 20 else "shelf (50–200 m likely)" if coast_km < 80 else "deep water likely",
        "wind_ms": wind_ms, "sea_state": None if wind_ms is None else ("calm" if wind_ms < 5 else "moderate" if wind_ms < 10 else "rough" if wind_ms < 15 else "severe"),
        "drift_speed": drift_speed, "drift_bearing": (math.degrees(math.atan2(vx, vy)) + 360) % 360 if drift_speed > 0 else None,
        "ext_km": max((b[2] - b[0]) * 111.32 * math.cos(math.radians(lat)), (b[3] - b[1]) * 110.574),
    }


def _tactical(s: dict) -> list:
    """Boom line perpendicular to drift at the down-drift edge (6 h lead) + skimmer at thickest zone."""
    if s["drift_bearing"] is None:
        return []
    brg, ext = s["drift_bearing"], s["ext_km"]
    edge_lat, edge_lon = destination(s["lat"], s["lon"], brg, ext / 2 + s["drift_speed"] * 6 * 3.6)
    out = []
    for i, off in enumerate([-0.6, 0, 0.6]):
        bl, bo = destination(edge_lat, edge_lon, (brg + 90) % 360, off * max(ext, 1.0))
        out.append({"id": f"boom-{i + 1}", "lat": round(bl, 5), "lon": round(bo, 5), "role": "containment boom anchor (down-drift line, 6 h lead)", "bearing_of_line_deg": round((brg + 90) % 360)})
    sk_lat, sk_lon = destination(s["lat"], s["lon"], brg, ext / 4)
    out.append({"id": "skimmer-1", "lat": round(sk_lat, 5), "lon": round(sk_lon, 5), "role": "skimmer / recovery vessel at thickest down-drift zone"})
    return out


def _tier1(s: dict) -> dict:
    coast, sea = s["coast_km"], s["sea_state"]
    return {"tier": 1, "title": "Containment & recovery", "priority": "immediate" if s["area"] >= 0.5 or coast < 30 else "standard",
            "actions": [f"Deploy {'ocean' if coast > 30 else 'harbour/inshore'} containment booms along the down-drift edge (see tactical coordinates); estimated slick extent {s['ext_km']:.1f} km.",
                        f"Position {'weir/brush' if s['thick_class'] == 'thick' else 'oleophilic drum'} skimmers with temporary storage ≥ {max(10, s['vol_m3']['thick'] * 0.3):.0f} m³.",
                        "Issue NAVTEX/notice to mariners; establish 3 nm exclusion zone; overflight/drone verification of slick edges within 2 h.",
                        "Log sample collection (fingerprinting) at 3 points for source identification and legal chain of custody."],
            "constraints": [f"Sea state {sea or 'unknown'} — booms lose effectiveness above ~1 m significant wave height / 10 m/s wind" if sea in (None, "rough", "severe") else f"Sea state {sea}: mechanical recovery viable."]}


def _tier2(s: dict) -> dict:
    coast, thick, wind_ms = s["coast_km"], s["thick_class"], s["wind_ms"]
    wind_ok = wind_ms is None or DISPERSANT_WIND[0] <= wind_ms <= DISPERSANT_WIND[1]
    shallow = "shallow" in s["depth_class"]
    dispersant_ok = coast >= 20 and not shallow and thick != "sheen" and wind_ok
    blockers = [r for r, bad in [("within 20 km of shore", coast < 20), ("shallow water", shallow), ("sheen too thin to disperse", thick == "sheen"), ("insufficient/excess wind for mixing", not wind_ok)] if bad]
    burn_ok = coast >= 50 and thick == "thick"
    return {"tier": 2, "title": "Chemical & biological treatment", "priority": "conditional",
            "dispersant": {"suitable": dispersant_ok, "reason": "Offshore (≥20 km), sufficient depth, fresh non-sheen oil and mixing energy" if dispersant_ok else ("; ".join(blockers) or "insufficient data"),
                           "note": "Requires national authority approval (e.g. NOS-DCP / regional contingency plan); never over reefs, mangroves, aquaculture or drinking-water intakes."},
            "in_situ_burning": {"suitable": burn_ok, "reason": "fresh thick oil far offshore" if burn_ok else "too thin / too near shore"},
            "bioremediation": {"suitable": coast < 30, "reason": "shoreline stranding likely — nutrient-enhanced bioremediation for sandy/gravel beaches; avoid pressure washing on rocky intertidal" if coast < 30 else "not applicable offshore"}}


def _tier3(t0) -> dict:
    return {"tier": 3, "title": "Restoration monitoring", "priority": "follow-up",
            "schedule": [{"when": (t0 + timedelta(hours=24)).isoformat(), "task": "Repeat SAR/optical acquisition request; update drift forecast"},
                         {"when": (t0 + timedelta(days=3)).isoformat(), "task": "Shoreline (SCAT) survey along threatened coast; water & sediment sampling"},
                         {"when": (t0 + timedelta(days=14)).isoformat(), "task": "Fisheries/aquaculture tissue sampling; seabird & turtle mortality census"},
                         {"when": (t0 + timedelta(days=90)).isoformat(), "task": "Sediment PAH re-sampling; mangrove/wetland recovery assessment; close or extend monitoring"}]}


def build_playbook(case: dict, spill: dict, result: Optional[dict]) -> dict:
    s = _situation(spill, result)
    eta_coast_h = round(s["coast_km"] / (s["drift_speed"] * 3.6), 1) if s["drift_speed"] > 0.02 and s["coast_km"] < 300 else None
    return {"version": PLAYBOOK_VERSION, "advisory": True, "disclaimer": "ADVISORY — algorithmic guidance from a rules engine, not an operational order. Validate with on-scene commander and the national contingency plan.",
            "inputs": {"area_km2": round(s["area"], 3), "detection_confidence": s["conf"], "thickness_class": s["thick_class"], "estimated_volume_m3": {k: round(v, 1) for k, v in s["vol_m3"].items()}, "estimated_volume_tonnes": s["vol_t"],
                       "coast_distance_km": s["coast_km"], "coast_note": s["coast_note"], "depth_class": s["depth_class"], "wind_ms": s["wind_ms"], "sea_state": s["sea_state"], "drift_speed_ms": round(s["drift_speed"], 3),
                       "drift_bearing_deg": round(s["drift_bearing"]) if s["drift_bearing"] is not None else None,
                       "eta_to_coast_hours": eta_coast_h, "primary_jurisdiction": (case.get("primary_jurisdiction") or {}).get("code")},
            "tactical_coordinates": _tactical(s), "tiers": [_tier1(s), _tier2(s), _tier3(to_utc(case["acquisition_time"]))]}


async def playbook_for_case(case_id: str) -> Optional[dict]:
    case = await db.cases.find_one({"id": case_id}, {"_id": 0})
    if not case:
        return None
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0})
    result = await db.correlation_results.find_one({"case_id": case_id}, {"_id": 0, "environment": 1}, sort=[("version", -1)])
    return build_playbook(case, spill, result)
