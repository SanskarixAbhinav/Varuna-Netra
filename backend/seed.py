from datetime import datetime, timedelta, timezone

from db import db
from geo import destination, rotated_rect_polygon
from models import SceneCreate, SpillObservationCreate, AISPositionIn, WindInput, CurrentInput
from services import create_scene, create_spill_observation, ingest_ais
from jobs import enqueue, process

T0 = datetime(2026, 6, 10, 5, 30, tzinfo=timezone.utc)
T2 = datetime(2026, 6, 11, 17, 45, tzinfo=timezone.utc)


def track(mmsi, name, imo, vtype, lat, lon, cog, sog_kn, t_center, k_from, k_to, step_min=10, skip=None, flags=None, source="satellite-ais"):
    out = []
    for k in range(k_from, k_to + 1):
        if skip and skip[0] <= k <= skip[1]:
            continue
        t = t_center + timedelta(minutes=k * step_min)
        dist_km = sog_kn * 1.852 * (k * step_min / 60)
        la, lo = destination(lat, lon, cog, dist_km) if dist_km >= 0 else destination(lat, lon, (cog + 180) % 360, -dist_km)
        out.append(AISPositionIn(mmsi=mmsi, vessel_name=name, imo=imo, vessel_type=vtype, timestamp=t, lat=round(la, 5), lon=round(lo, 5),
                                 sog_kn=sog_kn, cog_deg=cog, heading_deg=cog, source=source, quality_flags=(flags or {}).get(k, [])))
    return out


async def seed_demo():
    if await db.cases.count_documents({}) > 0:
        return {"seeded": False}
    scene1 = await create_scene(SceneCreate(
        provider="sentinel-1", provider_scene_id="S1A_IW_GRDH_1SDV_20260610T053000_DEMO", sensor_mode="IW", polarization="VV+VH",
        acquisition_time=T0, footprint={"type": "Polygon", "coordinates": [[[3.0, 53.0], [4.6, 53.0], [4.6, 54.0], [3.0, 54.0], [3.0, 53.0]]]},
        storage_ref="s3://sentinelmar-demo/scenes/S1A_20260610T053000.tiff", metadata={"orbit": "descending", "relative_orbit": 37}), "seed")
    scene2 = await create_scene(SceneCreate(
        provider="sentinel-1", provider_scene_id="S1B_IW_GRDH_1SDV_20260611T174500_DEMO", sensor_mode="IW", polarization="VV",
        acquisition_time=T2, footprint={"type": "Polygon", "coordinates": [[[4.0, 53.8], [5.2, 53.8], [5.2, 54.4], [4.0, 54.4], [4.0, 53.8]]]},
        storage_ref="s3://sentinelmar-demo/scenes/S1B_20260611T174500.tiff", metadata={"orbit": "ascending", "note": "no external polygon — run mock detector"}), "seed")

    spill_a, case_a = await create_spill_observation(SpillObservationCreate(
        scene_id=scene1["id"], geometry=rotated_rect_polygon(53.55, 3.70, 60, 6.0, 0.8), acquisition_time=T0, source="sentinel-1/external-analyst",
        detection_confidence=0.86, quality_flags=[], processing_version="external-polygon-1.0", estimated_age_hours=4,
        wind=WindInput(speed_ms=8, direction_deg=240), current=CurrentInput(speed_ms=0.3, direction_deg=45),
        notes="Elongated dark slick, linear shape consistent with moving-source discharge"), "seed")
    spill_b, case_b = await create_spill_observation(SpillObservationCreate(
        scene_id=scene1["id"], geometry=rotated_rect_polygon(53.20, 4.30, 120, 4.0, 3.0), acquisition_time=T0, source="sentinel-1/external-analyst",
        detection_confidence=0.32, quality_flags=["low_wind", "natural_seep_suspect"], processing_version="external-polygon-1.0",
        notes="Diffuse dark patch in low-wind area; possible lookalike"), "seed")
    spill_c, case_c = await create_spill_observation(SpillObservationCreate(
        scene_id=scene1["id"], geometry=rotated_rect_polygon(53.85, 3.25, 150, 3.0, 0.6), acquisition_time=T0, source="sentinel-1/external-analyst",
        detection_confidence=0.71, quality_flags=["uncertain_age"], processing_version="external-polygon-1.0",
        notes="Narrow slick with no environmental data attached"), "seed")

    positions = []
    positions += track("244123456", "NORDIC TRADER", "9483210", "tanker", 53.55, 3.70, 60, 12, T0 - timedelta(hours=3), -36, 18)
    positions += track("219876543", "BALTIC STAR", "9312345", "cargo", *destination(53.55, 3.70, 315, 9), 200, 14, T0 - timedelta(hours=10), -20, 20)
    positions += track("235112233", "SEA HARVEST", None, "fishing", 53.45, 3.95, 90, 0.3, T0 - timedelta(hours=6), -8, 8, step_min=45, skip=(-2, 2))
    positions += track("636998877", "ATLAS VOYAGER", "9556677", "tanker", *destination(53.55, 3.70, 150, 4), 60, 11, T0 - timedelta(hours=4, minutes=30), -21, 6,
                       skip=(-9, 3), flags={4: ["spoof_suspect"]})
    positions += track("257000123", "OCEAN RELAY", "9600001", "cargo", 53.56, 3.72, 230, 16, T0 + timedelta(hours=1), -5, 10)
    positions += track("211555777", "HANSA FREIGHT", "9700002", "cargo", 54.5, 5.5, 20, 13, T0 - timedelta(hours=5), -12, 12)
    positions += track("232004567", "CELTIC PIONEER", "9800003", "tanker", 54.10, 4.60, 95, 10, T2 - timedelta(hours=2), -24, 12)
    positions += track("305888999", "DELTA MARINER", "9900004", "cargo", *destination(54.10, 4.60, 30, 12), 180, 12, T2 - timedelta(hours=7), -12, 12)
    positions.append(positions[0])  # deliberate duplicate to demonstrate dedup
    summary = await ingest_ais(positions, "seed-batch-001", "seed")

    for case in (case_a, case_b, case_c):
        job = await enqueue("correlate", {"case_id": case["id"], "params": None}, "seed", inline=True)
        await process(job["id"])
    return {"seeded": True, "scenes": 2, "cases": 3, "ais": summary}
