"""LIVE-mode utilities: demo-data purge, Indian Sentinel-1 watch regions, real health checks, provenance. No silent demo fallback anywhere."""
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from db import db, audit
from models import new_id

STARTED_AT = time.time()
DEMO_SCENE_PREFIX = "s3://sentinelmar-demo/"
# DEMO MODE must be an explicit administrator decision — never the default (a missing variable in production means PRODUCTION).
DEMO_MODE = os.environ.get("DEMO_MODE", "false").strip().lower() in ("1", "true", "yes")
APP_ENV = (os.environ.get("APP_ENV") or os.environ.get("ENVIRONMENT") or ("demo" if DEMO_MODE else "production")).lower()
STAC_ROOT = "https://planetarycomputer.microsoft.com/api/stac/v1"
INDIA_EEZ_BBOX = [66.0, 5.5, 95.5, 24.5]

# name, bbox (W,S,E,N), note — real Indian operational areas polled every 3 h by the platform cron
INDIA_WATCHES = [
    ("Gulf of Kutch — Vadinar/Sikka terminals", [68.3, 22.2, 70.6, 23.4], "Largest Indian crude import terminals; Marine National Park adjacent"),
    ("Mumbai High offshore field", [71.5, 18.6, 73.2, 20.2], "ONGC platforms, pipelines, Mumbai port approaches"),
    ("Goa – New Mangalore lane", [72.8, 12.6, 74.8, 15.8], "Iron-ore/tanker traffic, Malvan & Netrani reefs"),
    ("Kochi – Lakshadweep", [72.0, 8.0, 76.6, 11.8], "Kochi refinery SPM, atoll lagoons"),
    ("Gulf of Mannar – Tuticorin", [77.5, 8.0, 80.3, 10.4], "Marine National Park, Sri Lanka strait traffic"),
    ("Chennai – Ennore – Kamarajar", [80.0, 12.4, 81.4, 13.9], "2017 & 2023 spill sites; Pulicat lagoon"),
    ("KG Basin offshore", [81.2, 15.4, 83.6, 17.2], "Gas/oil fields, Coringa mangroves"),
    ("Paradip – Haldia – Sundarbans", [86.0, 19.6, 89.4, 22.0], "Refinery ports, Bhitarkanika & Sundarbans"),
    ("Andaman & Nicobar", [91.5, 6.5, 94.5, 14.0], "Malacca approach traffic, coral reefs"),
]


async def seed_india_watches(actor: str = "system") -> int:
    if await db.scene_watches.count_documents({"seed": "india-live"}) > 0:
        return 0
    now = datetime.now(timezone.utc)
    docs = [{"id": new_id(), "name": n, "bbox": b, "collection": "sentinel-1-grd", "auto_detect": True, "active": True, "note": note, "seed": "india-live",
             "created_by": actor, "created_at": now, "last_polled": None, "runs": 0} for n, b, note in INDIA_WATCHES]
    await db.scene_watches.insert_many(docs)
    return len(docs)


async def purge_demo_data(actor: str) -> dict:
    """Remove seeded North Sea demo scenes, their spills/cases/results/AIS, keeping real Sentinel-1 cases, zones, gazetteer, archive, users."""
    scenes = await db.scenes.find({"storage_ref": {"$regex": f"^{DEMO_SCENE_PREFIX}"}}, {"id": 1}).to_list(50)
    scene_ids = [s["id"] for s in scenes]
    spills = await db.spill_observations.find({"scene_id": {"$in": scene_ids}}, {"id": 1}).to_list(500)
    spill_ids = [s["id"] for s in spills]
    cases = await db.cases.find({"spill_observation_id": {"$in": spill_ids}}, {"id": 1}).to_list(500)
    case_ids = [c["id"] for c in cases]
    # test/demo cases created against the North Sea demo box without a demo scene (integration-test artefacts).
    # Match by test/mock SOURCE or test SCENE only — never by geography alone, so genuine
    # detector/analyst/imported investigations that happen to sit in the North Sea are never removed.
    test_scenes = [s["id"] for s in await db.scenes.find({"$or": [{"storage_ref": {"$regex": "^s3://test/"}}, {"provider_scene_id": {"$regex": "^TEST_"}}]}, {"id": 1}).to_list(500)]
    scene_ids = list(set(scene_ids + test_scenes))
    extra_spills = [s["id"] for s in await db.spill_observations.find({"$or": [{"source": {"$in": ["test", "mock_detector"]}}, {"scene_id": {"$in": test_scenes}}]}, {"id": 1}).to_list(2000)]
    extra_cases = [c["id"] for c in await db.cases.find({"spill_observation_id": {"$in": extra_spills}}, {"id": 1}).to_list(2000)]
    spill_ids, case_ids = list(set(spill_ids + extra_spills)), list(set(case_ids + extra_cases))
    # SAFETY GUARD: never delete a genuine investigation (detector/analyst/imported) even if swept in above.
    genuine = {c["id"] for c in await db.cases.find({"id": {"$in": case_ids}, "origin": {"$in": ["detector", "analyst", "imported"]}}, {"id": 1}).to_list(2000)}
    if genuine:
        case_ids = [cid for cid in case_ids if cid not in genuine]
    demo_ais = {"source": {"$in": ["satellite-ais", "terrestrial-ais", "csv-upload"]}, "lat": {"$gte": 51.5, "$lte": 55.5}, "lon": {"$gte": 1.5, "$lte": 7.0},
                "timestamp": {"$gte": datetime(2026, 6, 8, tzinfo=timezone.utc), "$lte": datetime(2026, 6, 13, tzinfo=timezone.utc)}}
    out = {}
    for coll, q in [("correlation_results", {"case_id": {"$in": case_ids}}), ("reviews", {"case_id": {"$in": case_ids}}), ("alerts", {"case_id": {"$in": case_ids}}),
                    ("detector_feedback", {"case_id": {"$in": case_ids}}), ("dark_vessel_scans", {"case_id": {"$in": case_ids}}), ("case_vulnerability_osm", {"case_id": {"$in": case_ids}}),
                    ("attachments", {"case_id": {"$in": case_ids}}), ("timeline_shares", {"case_id": {"$in": case_ids}}), ("cases", {"id": {"$in": case_ids}}),
                    ("spill_observations", {"id": {"$in": spill_ids}}), ("scenes", {"id": {"$in": scene_ids}}), ("ais_positions", demo_ais)]:
        out[coll] = (await db[coll].delete_many(q)).deleted_count
    await db.settings.update_one({"key": "data_mode"}, {"$set": {"key": "data_mode", "demo_purged": True, "purged_at": datetime.now(timezone.utc), "purged_by": actor}}, upsert=True)
    await audit("system", "data_mode", "demo.purged", out, actor)
    return out


async def data_mode() -> dict:
    s = await db.settings.find_one({"key": "data_mode"}, {"_id": 0}) or {}
    demo_scenes = await db.scenes.count_documents({"storage_ref": {"$regex": f"^{DEMO_SCENE_PREFIX}"}})
    real_scenes = await db.scenes.count_documents({"provider_scene_id": {"$regex": "^S1"}, "storage_ref": {"$not": {"$regex": f"^{DEMO_SCENE_PREFIX}"}}})
    return {"demo_mode_env": DEMO_MODE, "environment": APP_ENV, "demo_data_present": demo_scenes > 0, "demo_purged": bool(s.get("demo_purged")), "real_scenes": real_scenes,
            "mode": "DEMO" if DEMO_MODE else "PRODUCTION", "note": None if not (demo_scenes > 0 and not DEMO_MODE) else "seeded demo scenes still present in the database — purge them (admin) to keep production counters clean",
            "purged_at": s.get("purged_at")}


async def ais_status_public() -> dict:
    import ais_live
    cov = await ais_live.get_coverage()
    st = await ais_live.status_async()
    return {**st, "coverage_mode": cov["mode"], "coverage_name": cov["name"], "coverage_bbox": cov["bboxes"], "key_configured": st["configured"], "reconnect_count": st["reconnects"],
            "note": None if st["connected"] else "Satellite analysis still operational; vessel attribution unavailable until AIS coverage is restored."}


async def stac_online() -> dict:
    t = time.time()
    try:
        async with httpx.AsyncClient(timeout=8) as cl:
            r = await cl.get(f"{STAC_ROOT}/collections/sentinel-1-grd")
        return {"online": r.status_code == 200, "latency_ms": int((time.time() - t) * 1000), "provider": "Microsoft Planetary Computer STAC"}
    except Exception as e:  # noqa: BLE001
        return {"online": False, "error": str(e)[:120], "provider": "Microsoft Planetary Computer STAC"}


async def system_health() -> dict:
    try:
        await db.command("ping")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    try:
        from lazy_libs import cv2
        ml = {"ready": bool(cv2.__version__), "model": "dark-spot Otsu/CFAR heuristics (EXPERIMENTAL, no trained ML)", "opencv": cv2.__version__}
    except Exception as e:  # noqa: BLE001
        ml = {"ready": False, "error": str(e)[:120]}
    last_scene = await db.scenes.find_one({"storage_ref": {"$not": {"$regex": f"^{DEMO_SCENE_PREFIX}"}}}, {"_id": 0, "provider_scene_id": 1, "acquisition_time": 1, "created_at": 1}, sort=[("created_at", -1)])
    last_ais = await db.ais_positions.find_one({"source": "AISStream"}, {"_id": 0, "timestamp": 1, "mmsi": 1}, sort=[("timestamp", -1)])
    day = datetime.now(timezone.utc) - timedelta(hours=24)
    return {"checked_at": datetime.now(timezone.utc), "uptime_s": int(time.time() - STARTED_AT), "database": {"online": db_ok}, "sentinel_stac": await stac_online(),
            "authentication": __import__("google_auth").capabilities()["authentication"],
            "ais": await ais_status_public(), "ml_inference": ml, "last_scene": last_scene, "last_ais": last_ais, "data_mode": await data_mode(),
            "last_24h": {"scenes_registered": await db.scenes.count_documents({"created_at": {"$gte": day}}), "detections": await db.cases.count_documents({"created_at": {"$gte": day}}),
                         "alerts": await db.alerts.count_documents({"created_at": {"$gte": day}}), "jobs": await db.jobs.count_documents({"created_at": {"$gte": day}})},
            "watches": {"active": await db.scene_watches.count_documents({"active": True}), "auto_detect": await db.scene_watches.count_documents({"active": True, "auto_detect": True})},
            "icg_districts": await db.icg_districts.count_documents({"active": True})}


async def provenance(case_id: str) -> dict:
    case = await db.cases.find_one({"id": case_id}, {"_id": 0})
    if not case:
        raise ValueError("case not found")
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0})
    scene = await db.scenes.find_one({"id": case.get("scene_id")}, {"_id": 0}) if case.get("scene_id") else None
    result = await db.correlation_results.find_one({"case_id": case_id}, {"_id": 0}, sort=[("version", -1)])
    is_demo = bool(scene and str(scene.get("storage_ref", "")).startswith(DEMO_SCENE_PREFIX))
    real_s1 = bool(scene and not is_demo and str(scene.get("provider_scene_id", "")).startswith("S1"))
    ais_src = await db.ais_positions.distinct("source", {"id": {"$in": (result or {}).get("candidates", [{}])[0].get("evidence", {}).get("fix_ids", [])[:200]}}) if result and result.get("candidates") else []
    ais_mode = "LIVE" if "AISStream" in ais_src else ("HISTORICAL" if ais_src and not is_demo else ("DEMO" if is_demo else "NONE"))
    pv, src = str(spill.get("processing_version", "")), str(spill.get("source", ""))
    det_badge = "EXPERIMENTAL" if ("darkspot" in pv or "dark_spot" in src) else ("MOCK" if "mock" in pv or "mock" in src else ("DEMO" if is_demo else "ANALYST"))
    ais_last = None
    if result and result.get("candidates"):
        fx = (result["candidates"][0].get("evidence") or {}).get("closest_fix") or {}
        ais_last = fx.get("timestamp")
    jz = case.get("primary_jurisdiction") or {}
    zone_doc = await db.jurisdictions.find_one({"code": jz.get("code")}, {"_id": 0, "provenance": 1, "source": 1, "dataset": 1, "authority_status": 1}) if jz.get("code") else None
    from jurisdiction import provenance_of
    att = case.get("scene_attachment") or {}
    return {"case_id": case_id, "case_number": case["case_number"], "case_origin": case.get("origin"), "case_origin_label": case.get("origin_label"),
            "satellite": {"provider": (scene or {}).get("metadata", {}).get("source") or (scene or {}).get("provider") or "external polygon", "scene_id": (scene or {}).get("provider_scene_id"),
                          "acquisition_time": (scene or {}).get("acquisition_time") or spill["acquisition_time"], "event_time": spill["acquisition_time"], "time_difference_hours": att.get("time_difference_hours"),
                          "platform": (scene or {}).get("metadata", {}).get("platform"), "orbit_state": (scene or {}).get("metadata", {}).get("orbit_state"),
                          "polarization": (scene or {}).get("polarization"), "sensor": "SAR (C-band)" if real_s1 else ("optical" if scene and str(scene.get("provider_scene_id", "")).startswith("S2") else None),
                          "analysis_asset": att.get("analysis_asset_key"), "stac_href": (scene or {}).get("storage_ref") if real_s1 else None,
                          "badge": "REAL SENTINEL-1" if real_s1 else ("DEMO" if is_demo else ("UNAVAILABLE" if not scene else "EXTERNAL")),
                          "status": "LATEST AVAILABLE ACQUISITION (archive, not real-time)" if real_s1 else ("no Sentinel scene attached — SAR confirmation pending" if not scene else None)},
            "detection": {"source": spill.get("source"), "model": spill.get("processing_version"), "confidence": spill.get("detection_confidence") if det_badge == "EXPERIMENTAL" else None,
                          "confidence_source": "detector" if det_badge == "EXPERIMENTAL" else "registrant_supplied",
                          "confidence_note": None if det_badge == "EXPERIMENTAL" else "N/A — value supplied at registration by the analyst/API caller, not produced by a detector", "badge": det_badge,
                          "type": "heuristic SAR dark-spot detector (OpenCV) — experimental" if det_badge == "EXPERIMENTAL" else ("analyst / API-registered polygon" if det_badge == "ANALYST" else det_badge.lower()),
                          "validation": "MODEL ACCURACY NOT YET VALIDATED AGAINST A LABELLED BENCHMARK DATASET"},
            "ais": {"provider": "AISStream" if ais_mode == "LIVE" else (", ".join(ais_src) or "none"), "mode": ais_mode, "status": await ais_status_public(), "observations": (result or {}).get("position_count", 0), "badge": ais_mode if ais_mode != "NONE" else "UNAVAILABLE",
                    "last_observation": ais_last, "note": "positions relative to the satellite acquisition time (historical window), not current positions" if ais_mode in ("LIVE", "HISTORICAL") else None},
            "jurisdiction": {"zone": jz.get("code"), "zone_name": jz.get("name"), "country": jz.get("country") or jz.get("country_code"), "authority": jz.get("authority"),
                             "dataset": ((zone_doc or {}).get("dataset") or {}).get("name") or (zone_doc or {}).get("source"), "badge": (provenance_of(zone_doc) if zone_doc else "UNAVAILABLE"),
                             "resolved_at": case.get("jurisdiction_resolved_at"), "note": "geographic intersection with a reference boundary — not a legal determination" if zone_doc else "no reference maritime zone intersects (high seas or zone not imported)"},
            "analysis": {"algorithm": (result or {}).get("algorithm_version"), "version": (result or {}).get("version"), "analysed_at": (result or {}).get("created_at"), "input_hash": (result or {}).get("input_hash"),
                         "candidates": len((result or {}).get("candidates", [])), "degraded": (result or {}).get("degraded"), "weights": ((result or {}).get("params") or {}).get("weights")},
            "data_mode": "DEMO" if is_demo else {"detector": "LIVE DETECTED", "analyst": "ANALYST CREATED", "imported": "IMPORTED HISTORICAL"}.get(case.get("origin"), "REFERENCE")}
