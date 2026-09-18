from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from auth import get_current_user, require_role
from csv_ingest import detect_mapping, read_csv, rows_to_positions, ALIASES
from db import db, clean, to_utc
from models import SceneCreate, SpillObservationCreate, AISBatch, new_id
from services import create_scene, create_spill_observation, ingest_ais, mock_detect
from jobs import enqueue

router = APIRouter()
MAX_CSV_BYTES = 25 * 1024 * 1024


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_CSV_BYTES:
        raise HTTPException(413, "CSV exceeds 25 MB limit")
    if not data.strip():
        raise HTTPException(400, "empty file")
    return data


@router.post("/ais/csv/preview")
async def ais_csv_preview(file: UploadFile = File(...), user=Depends(require_role("analyst"))):
    headers, rows = read_csv(await _read_upload(file))
    if not headers:
        raise HTTPException(400, "could not read CSV headers")
    mapping = detect_mapping(headers)
    sample_positions, sample_errors = rows_to_positions(rows[:20], mapping) if all(k in mapping for k in ("mmsi", "timestamp", "lat", "lon")) else ([], [])
    return {"filename": file.filename, "headers": headers, "row_count": len(rows), "detected_mapping": mapping,
            "missing_required": [k for k in ("mmsi", "timestamp", "lat", "lon") if k not in mapping], "fields": list(ALIASES.keys()),
            "preview_rows": rows[:5], "sample_parsed": [p.model_dump(mode="json") for p in sample_positions[:5]], "sample_errors": sample_errors[:5]}


@router.post("/ais/csv/ingest", status_code=201)
async def ais_csv_ingest(file: UploadFile = File(...), mapping: Optional[str] = Form(None), source: str = Form("csv-upload"), user=Depends(require_role("analyst"))):
    import json
    headers, rows = read_csv(await _read_upload(file))
    m = json.loads(mapping) if mapping else detect_mapping(headers)
    m = {k: v for k, v in m.items() if v}
    try:
        positions, errors = rows_to_positions(rows, m, source)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not positions:
        raise HTTPException(400, f"no valid rows parsed ({len(errors)} row errors)")
    batch_id = new_id()
    summary = await ingest_ais(positions, batch_id, user["email"])
    return {**summary, "filename": file.filename, "rows_total": len(rows), "row_errors": errors[:100], "row_error_count": len(errors), "mapping": m}


MAX_PROFILE_FIXES, MAX_PROFILE_RESULTS = 5000, 500


@router.get("/vessels/{mmsi}/profile")
async def vessel_profile(mmsi: str, user=Depends(get_current_user)):
    total_fixes = await db.ais_positions.count_documents({"mmsi": mmsi})
    if not total_fixes:
        raise HTTPException(404, "no AIS data for this MMSI")
    fixes = await db.ais_positions.find({"mmsi": mmsi}, {"_id": 0, "location": 0, "dedup_hash": 0}).sort("timestamp", -1).limit(MAX_PROFILE_FIXES).to_list(MAX_PROFILE_FIXES)
    fixes.reverse()
    latest = fixes[-1]
    gaps = sum(1 for a, b in zip(fixes, fixes[1:]) if (b["timestamp"] - a["timestamp"]).total_seconds() > 7200)
    flags = sorted({f for p in fixes for f in p.get("quality_flags", [])})
    names = sorted({p["vessel_name"] for p in fixes if p.get("vessel_name")})
    results = await db.correlation_results.find({"candidates.mmsi": mmsi}, {"_id": 0, "case_id": 1, "version": 1, "created_at": 1, "candidates": 1, "overall_status": 1}).sort("version", -1).limit(MAX_PROFILE_RESULTS).to_list(MAX_PROFILE_RESULTS)
    latest_by_case = {}
    for r in results:
        latest_by_case.setdefault(r["case_id"], r)
    cases = await db.cases.find({"id": {"$in": list(latest_by_case)[:MAX_PROFILE_RESULTS]}}, {"_id": 0}).to_list(MAX_PROFILE_RESULTS)
    appearances = []
    for c in cases:
        r = latest_by_case[c["id"]]
        cand = next(x for x in r["candidates"] if x["mmsi"] == mmsi)
        appearances.append({"case_id": c["id"], "case_number": c["case_number"], "acquisition_time": c["acquisition_time"], "result_version": r["version"],
                            "rank": cand["rank"], "score": cand["score"], "candidate_status": cand["status"], "candidate_count": len(r["candidates"]),
                            "case_attribution_status": c["attribution_status"], "review_state": c["review_state"],
                            "confirmed_this_vessel": c.get("confirmed_vessel_mmsi") == mmsi, "primary_jurisdiction": (c.get("primary_jurisdiction") or {}).get("code"),
                            "distance_km": cand["evidence"]["distance_km"], "time_gap_hours": cand["evidence"]["time_gap_hours"]})
    appearances.sort(key=lambda a: a["acquisition_time"], reverse=True)
    decisions = await db.reviews.find({"vessel_mmsi": mmsi}, {"_id": 0}).sort("created_at", -1).to_list(500)
    case_numbers = {c["id"]: c["case_number"] for c in await db.cases.find({"id": {"$in": [d["case_id"] for d in decisions]}}, {"_id": 0, "id": 1, "case_number": 1}).to_list(500)}
    for d in decisions:
        d["case_number"] = case_numbers.get(d["case_id"])
    return clean({
        "mmsi": mmsi, "vessel_name": latest.get("vessel_name"), "imo": latest.get("imo"), "vessel_type": latest.get("vessel_type"), "name_variants": names,
        "ais_summary": {"fixes": total_fixes, "fixes_analysed": len(fixes), "first_seen": fixes[0]["timestamp"], "last_seen": latest["timestamp"], "sources": sorted({p.get("source") for p in fixes if p.get("source")}),
                        "quality_flags": flags, "gaps_over_2h": gaps, "last_position": {"lat": latest["lat"], "lon": latest["lon"], "sog_kn": latest.get("sog_kn"), "cog_deg": latest.get("cog_deg")}},
        "summary": {"appearances": len(appearances), "probable_or_confirmed": sum(1 for a in appearances if a["candidate_status"] == "probable" or a["confirmed_this_vessel"]),
                    "confirmed": sum(1 for d in decisions if d["decision"] == "confirm"), "rejected": sum(1 for d in decisions if d["decision"] == "reject"),
                    "top_ranked": sum(1 for a in appearances if a["rank"] == 1)},
        "appearances": appearances, "decisions": decisions,
        "disclaimer": "Historical appearances are correlation candidates only; repeated appearance is not evidence of responsibility.",
    })


@router.post("/scenes", status_code=201)
async def register_scene(payload: SceneCreate, user=Depends(require_role("analyst"))):
    try:
        return clean(await create_scene(payload, user["email"]))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/scenes")
async def list_scenes(limit: int = Query(100, le=500), user=Depends(get_current_user)):
    return clean(await db.scenes.find({}, {"_id": 0}).sort("acquisition_time", -1).to_list(limit))


@router.get("/scenes/{scene_id}")
async def get_scene(scene_id: str, user=Depends(get_current_user)):
    doc = await db.scenes.find_one({"id": scene_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "scene not found")
    spills = await db.spill_observations.find({"scene_id": scene_id}, {"_id": 0, "raw_input": 0}).to_list(100)
    return clean({**doc, "spill_observations": spills})


@router.post("/scenes/{scene_id}/detect", status_code=201)
async def detect_scene(scene_id: str, correlate: bool = True, user=Depends(require_role("analyst"))):
    """Real Sentinel STAC scene → experimental dark-spot detector on real SAR. Mock placeholder only when DEMO_MODE and demo data not purged."""
    scene = await db.scenes.find_one({"id": scene_id}, {"_id": 0})
    if not scene:
        raise HTTPException(404, "scene not found")
    scene["acquisition_time"] = to_utc(scene["acquisition_time"])
    if (scene.get("metadata") or {}).get("stac_collection"):
        from detector import run_dark_spot_detector
        try:
            det = await run_dark_spot_detector(scene, user["email"])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Dark-spot detector failed: {str(e)[:200]}")
        jobs_out = [await enqueue("correlate", {"case_id": c["case_id"], "params": None}, user["email"]) for c in det["cases"]] if correlate else []
        first = det["cases"][0] if det["cases"] else None
        case = await db.cases.find_one({"id": first["case_id"]}, {"_id": 0}) if first else None
        return clean({"detection": det, "case": case, "job": jobs_out[0] if jobs_out else None, "jobs": jobs_out, "detector_note": det["note"]})
    from livemode import data_mode
    dm = await data_mode()
    if dm["mode"] == "LIVE" or dm["demo_purged"]:
        raise HTTPException(409, "Mock detection is disabled in LIVE mode — register a real Sentinel-1 scene from Scene Explorer and run the dark-spot detector on its SAR asset")
    spill, case = await mock_detect(scene, user["email"])
    job = await enqueue("correlate", {"case_id": case["id"], "params": None}, user["email"]) if correlate else None
    return clean({"spill_observation": spill, "case": case, "job": job, "detector_note": "Mock detector output — DEMO placeholder"})


@router.post("/spill-observations", status_code=201)
async def create_spill(payload: SpillObservationCreate, correlate: bool = False, user=Depends(require_role("analyst"))):
    try:
        spill, case = await create_spill_observation(payload, user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    job = await enqueue("correlate", {"case_id": case["id"], "params": None}, user["email"]) if correlate else None
    return clean({"spill_observation": spill, "case": case, "job": job})


@router.get("/spill-observations")
async def list_spills(limit: int = Query(100, le=500), user=Depends(get_current_user)):
    return clean(await db.spill_observations.find({}, {"_id": 0, "raw_input": 0}).sort("acquisition_time", -1).to_list(limit))


@router.get("/spill-observations/{spill_id}")
async def get_spill(spill_id: str, user=Depends(get_current_user)):
    doc = await db.spill_observations.find_one({"id": spill_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "spill observation not found")
    return clean(doc)


@router.post("/ais/positions", status_code=201)
async def ingest_positions(batch: AISBatch, user=Depends(require_role("analyst"))):
    return await ingest_ais(batch.positions, new_id(), user["email"])


@router.get("/ais/positions")
async def query_positions(mmsi: Optional[str] = None, start: Optional[datetime] = None, end: Optional[datetime] = None,
                          limit: int = Query(1000, le=10000), user=Depends(get_current_user)):
    q = {}
    if mmsi:
        q["mmsi"] = mmsi
    if start or end:
        q["timestamp"] = {}
        if start:
            q["timestamp"]["$gte"] = to_utc(start)
        if end:
            q["timestamp"]["$lte"] = to_utc(end)
    return clean(await db.ais_positions.find(q, {"_id": 0, "location": 0, "dedup_hash": 0}).sort("timestamp", 1).to_list(limit))
