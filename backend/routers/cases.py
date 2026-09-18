from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from auth import get_current_user, require_role
from db import db, clean, audit, to_utc
from geo import circle_polygon
from models import CorrelateRequest, ReviewCreate, OverrideRequest, REASON_CODES, new_id
from jobs import enqueue, process
from services import apply_live_environment
from report import build_pdf
from storage import get_object

router = APIRouter()


async def _case(case_id):
    case = await db.cases.find_one({"id": case_id}, {"_id": 0})
    if not case:
        raise HTTPException(404, "case not found")
    return case


async def _result(case_id, version: Optional[int]):
    q = {"case_id": case_id}
    if version:
        q["version"] = version
    return await db.correlation_results.find_one(q, {"_id": 0}, sort=[("version", -1)])


@router.get("/demo/reference")
async def get_reference_case(user=Depends(get_current_user)):
    """The admin-pinned SIH reference case. Never falls back to an arbitrary case."""
    pin = await db.settings.find_one({"key": "sih_reference_case"}, {"_id": 0})
    if not pin:
        return {"pinned": False, "available": False, "state": "REFERENCE CASE NOT PINNED", "case_id": None}
    case = await db.cases.find_one({"id": pin["case_id"]}, {"_id": 0, "id": 1, "case_number": 1, "origin": 1, "origin_label": 1})
    return clean({"pinned": True, "available": bool(case), "state": "REFERENCE CASE — STORED DATA" if case else "REFERENCE CASE UNAVAILABLE", "case_id": pin["case_id"],
                  "case_number": (case or {}).get("case_number") or pin.get("case_number"), "origin": (case or {}).get("origin"), "pinned_by": pin.get("pinned_by"), "pinned_at": pin.get("pinned_at")})


@router.put("/demo/reference/{case_id}")
async def pin_reference_case(case_id: str, user=Depends(require_role("admin"))):
    case = await _case(case_id)
    doc = {"key": "sih_reference_case", "case_id": case_id, "case_number": case["case_number"], "pinned_by": user["email"], "pinned_at": datetime.now(timezone.utc)}
    await db.settings.update_one({"key": "sih_reference_case"}, {"$set": doc}, upsert=True)
    await audit("settings", "sih_reference_case", "demo.reference_pinned", {"case_id": case_id, "case_number": case["case_number"]}, user["email"])
    return await get_reference_case(user)


@router.delete("/demo/reference")
async def unpin_reference_case(user=Depends(require_role("admin"))):
    await db.settings.delete_one({"key": "sih_reference_case"})
    await audit("settings", "sih_reference_case", "demo.reference_unpinned", {}, user["email"])
    return {"pinned": False}


@router.post("/cases/analyze-eligible", status_code=202)
async def analyze_eligible(limit: int = Query(200, le=1000), user=Depends(require_role("supervisor"))):
    """Queue correlation ONLY for real cases that have never been analysed AND have the inputs a run needs; the rest are tagged NOT ANALYZABLE with the reason. Idempotent — existing results are never re-run or overwritten."""
    from dashboard import REAL_CASE_FILTER, analyzability
    rows = await db.cases.find({**REAL_CASE_FILTER, "$or": [{"latest_result_version": {"$in": [0, None]}}, {"latest_result_version": {"$exists": False}}]}, {"_id": 0, "id": 1, "case_number": 1, "spill_observation_id": 1}).sort("acquisition_time", -1).to_list(limit)
    queued, skipped = [], []
    for c in rows:
        if await db.jobs.find_one({"type": "correlate", "payload.case_id": c["id"], "status": {"$in": ["queued", "running"]}}, {"_id": 1}):
            continue
        reason = await analyzability(db, c)
        if reason:
            await db.cases.update_one({"id": c["id"]}, {"$set": {"not_analyzable_reason": reason, "analyzability_checked_at": datetime.now(timezone.utc)}})
            skipped.append({"case_number": c["case_number"], "reason": reason})
            continue
        await db.cases.update_one({"id": c["id"]}, {"$unset": {"not_analyzable_reason": ""}})
        job = await enqueue("correlate", {"case_id": c["id"], "params": {}, "fetch_environment": False}, user["email"])
        queued.append({"case_number": c["case_number"], "job_id": job["id"]})
    await audit("cases", "batch", "cases.analyze_eligible", {"queued": len(queued), "skipped": len(skipped)}, user["email"])
    return clean({"candidates_checked": len(rows), "queued": queued, "skipped": skipped})


@router.get("/cases/{case_id}/evidence-timeline")
async def evidence_timeline(case_id: str, user=Depends(get_current_user)):
    """Chronological events built ONLY from stored timestamps (scene, spill, top-candidate track, correlation, reviews, case). Missing events are listed as unavailable — never invented."""
    case = await _case(case_id)
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "acquisition_time": 1, "created_at": 1, "source": 1, "processing_version": 1})
    scene = await db.scenes.find_one({"id": case["scene_id"]}, {"_id": 0, "acquisition_time": 1, "provider_scene_id": 1}) if case.get("scene_id") else None
    result = await db.correlation_results.find_one({"case_id": case_id}, {"_id": 0, "created_at": 1, "version": 1, "overall_status": 1, "candidates": {"$slice": 1}}, sort=[("version", -1)])
    reviews = await db.reviews.find({"case_id": case_id}, {"_id": 0, "created_at": 1, "decision": 1, "analyst": 1}).sort("created_at", 1).to_list(50)
    ev, missing = [], []
    def add(kind, label, t, detail=None, source=None):
        if t is None:
            missing.append({"kind": kind, "label": label, "state": "Not available"})
        else:
            ev.append({"kind": kind, "label": label, "time": t, "detail": detail, "source": source})
    add("scene_acquisition", "Sentinel-1 acquisition", (scene or {}).get("acquisition_time"), (scene or {}).get("provider_scene_id"), "scenes")
    add("detection", "Spill candidate detected / registered", (spill or {}).get("created_at"), f"{(spill or {}).get('source')} · {(spill or {}).get('processing_version')}", "spill_observations")
    top = (result or {}).get("candidates") or []
    if top:
        t = top[0]
        track = [f for f in t.get("track") or [] if not f.get("interpolated")]
        name = t.get("vessel_name") or f"MMSI {t['mmsi']}"
        add("corridor_entry", f"AIS corridor entry — {name}", track[0]["timestamp"] if track else None, f"{len(track)} real AIS fixes", "ais_positions")
        add("closest_approach", f"Closest approach — {name}", (t.get("evidence") or {}).get("closest_fix", {}).get("timestamp"), f"{(t.get('evidence') or {}).get('distance_km')} km · score {t.get('score')}", "correlation_results")
        add("corridor_exit", f"AIS corridor exit — {name}", track[-1]["timestamp"] if len(track) > 1 else None, None, "ais_positions")
    else:
        for k, l in (("corridor_entry", "AIS corridor entry"), ("closest_approach", "Closest vessel approach"), ("corridor_exit", "AIS corridor exit")):
            missing.append({"kind": k, "label": l, "state": "Not available — no AIS candidate" if result else "Not available — not analysed"})
    add("correlation", f"Correlation run v{(result or {}).get('version')}" if result else "Correlation run", (result or {}).get("created_at"), (result or {}).get("overall_status"), "correlation_results")
    for r in reviews:
        add("review", f"Analyst review — {r.get('decision')}", r.get("created_at"), r.get("analyst"), "reviews")
    if not reviews:
        missing.append({"kind": "review", "label": "Analyst review", "state": "Not available — no decision recorded"})
    add("case_created", "Case opened", case.get("created_at"), case.get("case_number"), "cases")
    if case.get("updated_at") and case.get("updated_at") != case.get("created_at"):
        add("case_updated", "Case last updated", case.get("updated_at"), None, "cases")
    ev.sort(key=lambda e: to_utc(e["time"]))
    return clean({"case_id": case_id, "case_number": case["case_number"], "events": ev, "unavailable": missing, "note": "every timestamp is read from stored records; nothing is estimated"})


@router.get("/cases")
async def list_cases(status: Optional[str] = None, attribution_status: Optional[str] = None, review_state: Optional[str] = None, origin: str = "real", include_demo: bool = False,
                     limit: int = Query(200, le=1000), user=Depends(get_current_user)):
    """origin: real (default — detector/analyst cases on real scenes) | imported | demo | all."""
    from dashboard import REAL_CASE_FILTER, REAL_ORIGINS
    if include_demo or origin == "all":
        q = {}
    elif origin == "real":
        q = dict(REAL_CASE_FILTER)
    elif origin in ("imported", "demo", "reference", "detector", "analyst"):
        q = {"origin": origin}
    else:
        raise HTTPException(400, f"origin must be one of real, imported, demo, all (real = {REAL_ORIGINS})")
    if status:
        q["status"] = status
    if attribution_status:
        q["attribution_status"] = {"$in": attribution_status.split(",")}
    if review_state:
        q["review_state"] = review_state
    from dashboard import annotate_cases
    return clean(await annotate_cases(db, await db.cases.find(q, {"_id": 0}).sort("acquisition_time", -1).to_list(limit)))


@router.get("/cases/{case_id}")
async def get_case(case_id: str, user=Depends(get_current_user)):
    case = await _case(case_id)
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "raw_input": 0})
    scene = await db.scenes.find_one({"id": case["scene_id"]}, {"_id": 0}) if case.get("scene_id") else None
    versions = await db.correlation_results.find({"case_id": case_id}, {"_id": 0, "version": 1, "created_at": 1, "overall_status": 1, "algorithm_version": 1, "input_hash": 1, "degraded": 1}).sort("version", 1).to_list(100)
    from sentinel_assets import scene_status_summary
    return clean({**case, "spill_observation": spill, "scene": scene, "scene_status": scene_status_summary(scene, case), "result_versions": versions})


@router.get("/cases/{case_id}/response-eta")
async def response_eta(case_id: str, user=Depends(get_current_user)):
    from response_eta import nearest_response
    case = await _case(case_id)
    lon, lat = case["centroid"]["coordinates"]
    out = nearest_response(lat, lon)
    await db.cases.update_one({"id": case_id}, {"$set": {"response_eta": out}})
    return clean({"case_id": case_id, "spill": {"lat": lat, "lon": lon}, **out})


@router.get("/cases/{case_id}/scene-timeline")
async def scene_timeline(case_id: str, days: int = Query(60, ge=1, le=365), user=Depends(get_current_user)):
    """Every real Sentinel-1 GRD pass over the spill centroid within ±days (Planetary Computer STAC) — for scrubbing slick evolution."""
    from satellite import search_scenes
    case = await _case(case_id)
    lon, lat = case["centroid"]["coordinates"]
    t = to_utc(case["acquisition_time"])
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        res = await search_scenes([lon - 0.02, lat - 0.02, lon + 0.02, lat + 0.02], (t - timedelta(days=days)).strftime(fmt), (t + timedelta(days=days)).strftime(fmt), "sentinel-1-grd", 100)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"STAC search failed: {str(e)[:160]}")
    reg = {s["provider_scene_id"]: s["id"] for s in await db.scenes.find({"provider_scene_id": {"$in": [x["stac_id"] for x in res["scenes"]]}}, {"_id": 0, "id": 1, "provider_scene_id": 1}).to_list(200)}
    passes = sorted(res["scenes"], key=lambda s: s["datetime"])
    for p in passes:
        dt = datetime.fromisoformat(p["datetime"].replace("Z", "+00:00"))
        p.update({"offset_hours": round((dt - t).total_seconds() / 3600, 1), "is_case_scene": reg.get(p["stac_id"]) == case.get("scene_id"), "registered_scene_id": reg.get(p["stac_id"]),
                  "preview": f"/satellite/preview?collection={p['collection']}&stac_id={p['stac_id']}" if p.get("preview_href") or p.get("thumbnail_href") else None})
    return clean({"case_id": case_id, "case_number": case["case_number"], "acquisition_time": t, "window_days": days, "count": len(passes), "passes": passes,
                  "source": "Microsoft Planetary Computer STAC — real archive acquisitions (near-real-time, not live)"})


class AttachScene(BaseModel):
    scene_id: Optional[str] = None


@router.post("/cases/{case_id}/attach-scene")
async def attach_case_scene(case_id: str, body: AttachScene = AttachScene(), user=Depends(require_role("analyst"))):
    """Attach a Sentinel scene by internal id / STAC id (registering it from STAC if needed) or auto-attach the best-ranked real Sentinel-1 scene (adaptive ±36 h→±7 d)."""
    from sentinel_assets import attach_scene, auto_attach_scene, resolve_scene_assets, register_stac_item, SceneAssetError
    from satellite import get_item
    case = await _case(case_id)
    try:
        if body.scene_id:
            scene = await db.scenes.find_one({"$or": [{"id": body.scene_id}, {"provider_scene_id": body.scene_id}]}, {"_id": 0})
            if not scene and body.scene_id.startswith("S1"):
                try:
                    scene = await register_stac_item(await get_item("sentinel-1-grd", body.scene_id), user["email"])
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(409, f"ATTACH_FAILED: scene {body.scene_id} not found in STAC ({str(e)[:100]})")
            if not scene:
                raise HTTPException(404, "scene not found")
            await attach_scene(case, scene, user["email"], window_h=(case.get("scene_search") or {}).get("search_window_hours"))
        else:
            scene = await auto_attach_scene(case, user["email"])
        status = await resolve_scene_assets(scene)
    except SceneAssetError as e:
        raise HTTPException(409, str(e))
    fresh = await _case(case_id)
    return clean({"case_id": case_id, "scene": scene, "scene_status": {**status, **{k: fresh["scene_attachment"].get(k) for k in ("time_difference_hours", "overlap_percent", "search_window_hours", "auto")}}, "attachment": fresh["scene_attachment"]})


@router.get("/cases/{case_id}/scene-candidates")
async def scene_candidates(case_id: str, max_hours: int = Query(168, ge=1, le=720), user=Depends(get_current_user)):
    """AVAILABLE SENTINEL-1 SCENES for this spill: adaptive staged search, ranked; supplementary Sentinel-2 optical context listed separately (never a SAR substitute)."""
    from sentinel_assets import nearest_scenes, _remember_search, SceneAssetError
    from satellite import search_scenes
    case = await _case(case_id)
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "geometry": 1, "acquisition_time": 1})
    t = to_utc(spill["acquisition_time"])
    try:
        res = await nearest_scenes(spill, t, max_hours)
    except SceneAssetError as e:
        raise HTTPException(400, str(e))
    await _remember_search(case_id, res)
    s2 = None
    try:
        r2 = await search_scenes(None, (t - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ"), (t + timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ"), "sentinel-2-l2a", 5, intersects=res["aoi"])
        s2 = {"role": "SUPPLEMENTARY — Sentinel-2 optical (not a SAR substitute)", "count": r2["count"], "scenes": [{k: x.get(k) for k in ("stac_id", "datetime", "cloud_cover", "platform")} for x in r2["scenes"]]}
    except Exception as e:  # noqa: BLE001
        s2 = {"role": "SUPPLEMENTARY — Sentinel-2 optical", "error": f"search failed: {str(e)[:100]}"}
    cands = [{k: v for k, v in c.items() if k != "_item"} for c in res.get("candidates", [])]
    return clean({"case_id": case_id, "target_time": t, "primary": "Sentinel-1 SAR (sentinel-1-grd)", "found": res["found"], "state": res["state"], "reason": res.get("reason"), "search_window_hours": res.get("search_window_hours"),
                  "stages_tried": res["stages_tried"], "candidates": cands, "attached_scene_id": case.get("scene_id"), "attachment": case.get("scene_attachment"), "supplementary": s2,
                  "source": "Microsoft Planetary Computer STAC — real archive acquisitions (latest available, not live)"})


@router.post("/cases/{case_id}/correlate", status_code=202)
async def correlate_case(case_id: str, body: CorrelateRequest = CorrelateRequest(), user=Depends(require_role("analyst"))):
    await _case(case_id)
    payload = {"case_id": case_id, "params": body.params.model_dump() if body.params else None, "fetch_environment": body.fetch_environment}
    job = await enqueue("correlate", payload, user["email"], inline=body.sync)
    await audit("case", case_id, "case.correlation_requested", {"job_id": job["id"], "sync": body.sync, "fetch_environment": body.fetch_environment}, user["email"])
    if body.sync:
        job = await process(job["id"])
    return clean(job)


@router.post("/cases/{case_id}/environment/fetch")
async def fetch_case_environment(case_id: str, user=Depends(require_role("analyst"))):
    case = await _case(case_id)
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "raw_input": 0})
    env = await apply_live_environment(spill, user["email"])
    return clean({"environment": env, "wind": spill.get("wind"), "current": spill.get("current")})


@router.get("/cases/{case_id}/candidates")
async def get_candidates(case_id: str, version: Optional[int] = None, include_tracks: bool = False, user=Depends(get_current_user)):
    case = await _case(case_id)
    result = await _result(case_id, version)
    if not result:
        return {"case_id": case_id, "version": 0, "candidates": [], "overall_status": case["attribution_status"], "message": "no correlation run yet"}
    if not include_tracks:
        for c in result["candidates"]:
            c.pop("track", None)
    mmsis = [c["mmsi"] for c in result["candidates"]]
    watch = {w["mmsi"]: w for w in await db.watchlist.find({"active": True, "mmsi": {"$in": mmsis}}, {"_id": 0, "mmsi": 1, "reason": 1, "severity": 1, "id": 1}).to_list(len(mmsis) or 1)}
    for c in result["candidates"]:
        c["watchlist"] = watch.get(c["mmsi"])
    result.pop("processing_log", None)
    return clean({"case_id": case_id, **result, "attribution_status": case["attribution_status"], "review_state": case["review_state"],
                  "disclaimer": "Ranked candidates are decision-support output from AIS/satellite correlation, not a legal determination of responsibility."})


@router.post("/cases/{case_id}/review", status_code=201)
async def review_case(case_id: str, body: ReviewCreate, user=Depends(require_role("analyst"))):
    case = await _case(case_id)
    bad = [c for c in body.reason_codes if c not in REASON_CODES]
    if bad:
        raise HTTPException(400, f"unknown reason codes: {bad}")
    result = await _result(case_id, body.result_version)
    if body.decision == "confirm":
        if not body.vessel_mmsi:
            raise HTTPException(400, "vessel_mmsi is required to confirm")
        if not result or body.vessel_mmsi not in {c["mmsi"] for c in result["candidates"]}:
            raise HTTPException(400, "vessel_mmsi must be a ranked candidate in the referenced result version")
    now = datetime.now(timezone.utc)
    review = {"id": new_id(), "case_id": case_id, "result_version": result["version"] if result else None, "decision": body.decision,
              "vessel_mmsi": body.vessel_mmsi, "reason_codes": body.reason_codes, "notes": body.notes,
              "analyst": user["email"], "analyst_name": user.get("name"), "analyst_role": user["role"], "analyst_id": user["id"],
              "previous_attribution_status": case["attribution_status"], "created_at": now}
    await db.reviews.insert_one(dict(review))
    update = {"updated_at": now}
    if body.decision == "confirm":
        update.update({"attribution_status": "analyst_confirmed", "review_state": "confirmed", "confirmed_vessel_mmsi": body.vessel_mmsi, "status": "closed"})
    elif body.decision == "reject":
        update.update({"attribution_status": "insufficient_evidence", "review_state": "rejected", "confirmed_vessel_mmsi": None, "status": "closed"})
    else:
        update.update({"review_state": "needs_more_data", "status": "under_review"})
    await db.cases.update_one({"id": case_id}, {"$set": update})
    await audit("case", case_id, f"review.{body.decision}", {"review_id": review["id"], "vessel_mmsi": body.vessel_mmsi, "reason_codes": body.reason_codes,
                                                          "result_version": review["result_version"], "from": case["attribution_status"],
                                                          "to": update.get("attribution_status", case["attribution_status"]), "role": user["role"]}, user["email"])
    review.pop("_id", None)
    return clean(review)


@router.post("/cases/{case_id}/override", status_code=201)
async def override_case(case_id: str, body: OverrideRequest, user=Depends(require_role("supervisor"))):
    case = await _case(case_id)
    now = datetime.now(timezone.utc)
    result = await _result(case_id, None)
    review = {"id": new_id(), "case_id": case_id, "result_version": result["version"] if result else None, "decision": "supervisor_override",
              "vessel_mmsi": body.vessel_mmsi, "reason_codes": [], "notes": body.notes, "analyst": user["email"], "analyst_name": user.get("name"),
              "analyst_role": user["role"], "analyst_id": user["id"], "previous_attribution_status": case["attribution_status"],
              "override_to": body.attribution_status, "created_at": now}
    await db.reviews.insert_one(dict(review))
    update = {"attribution_status": body.attribution_status, "review_state": "supervisor_override", "updated_at": now,
              "confirmed_vessel_mmsi": body.vessel_mmsi if body.attribution_status == "analyst_confirmed" else case.get("confirmed_vessel_mmsi")}
    if body.close_case:
        update["status"] = "closed"
    await db.cases.update_one({"id": case_id}, {"$set": update})
    await audit("case", case_id, "review.supervisor_override", {"review_id": review["id"], "from": case["attribution_status"], "to": body.attribution_status,
                                                              "vessel_mmsi": body.vessel_mmsi, "closed": body.close_case, "role": user["role"]}, user["email"])
    review.pop("_id", None)
    return clean(review)


@router.get("/cases/{case_id}/reviews")
async def list_reviews(case_id: str, user=Depends(get_current_user)):
    await _case(case_id)
    return clean(await db.reviews.find({"case_id": case_id}, {"_id": 0}).sort("created_at", 1).to_list(500))


def _geojson(case, spill, result):
    features = [{"type": "Feature", "geometry": spill["geometry"], "properties": {"layer": "spill", "id": spill["id"], "case_number": case["case_number"],
                 "acquisition_time": spill["acquisition_time"], "detection_confidence": spill["detection_confidence"], "quality_flags": spill["quality_flags"],
                 "estimated_area_km2": spill["estimated_area_km2"], "source": spill["source"]}}]
    if result:
        lon, lat = spill["centroid"]["coordinates"]
        features.append({"type": "Feature", "geometry": circle_polygon(lat, lon, result["params"]["corridor_km"] + spill.get("extent_km", 0)),
                         "properties": {"layer": "corridor", "radius_km": result["params"]["corridor_km"] + spill.get("extent_km", 0)}})
        dm = result.get("drift_model")
        if dm:
            features.append({"type": "Feature", "geometry": dm["envelope"], "properties": {"layer": "drift_envelope", "hours": dm["hours"], "k_sigma": dm["k_sigma"], "version": dm["version"]}})
            features.append({"type": "Feature", "geometry": dm["likely_envelope"], "properties": {"layer": "drift_likely", "window_hours": dm["likely_window_hours"]}})
            features.append({"type": "Feature", "geometry": dm["path"], "properties": {"layer": "drift_path", "hours": dm["path_hours"], "sigma_km": dm["sigma_km"], "speed_ms": dm["drift_speed_ms"]}})
        for c in result["candidates"]:
            props = {"layer": "track", "mmsi": c["mmsi"], "vessel_name": c.get("vessel_name"), "rank": c["rank"], "score": c["score"], "status": c["status"]}
            track = c.get("track", [])
            if len(track) >= 2:
                # split into solid (real) and dashed (interpolated) segments
                segs, cur, cur_interp = [], [track[0]], bool(track[0].get("interpolated"))
                for p in track[1:]:
                    interp = bool(p.get("interpolated"))
                    if interp != cur_interp:
                        cur.append(p)
                        segs.append((cur_interp, cur))
                        cur, cur_interp = [p], interp
                    else:
                        cur.append(p)
                segs.append((cur_interp, cur))
                for i, (interp, pts) in enumerate(segs):
                    if len(pts) < 2:
                        continue
                    features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in pts]},
                                     "properties": {**props, "segment": i, "interpolated": interp, "timestamps": [p["timestamp"] for p in pts], "sog": [p.get("sog_kn") for p in pts], "cog": [p.get("cog_deg") for p in pts]}})
            cf = c["evidence"]["closest_fix"]
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [cf["lon"], cf["lat"]]},
                             "properties": {**props, "layer": "closest_fix", "timestamp": cf["timestamp"], "distance_km": c["evidence"]["distance_km"],
                                            "time_gap_hours": c["evidence"]["time_gap_hours"], "sog_kn": cf.get("sog_kn"), "cog_deg": cf.get("cog_deg")}})
            if c["evidence"].get("backprojected_centroid"):
                features.append({"type": "Feature", "geometry": c["evidence"]["backprojected_centroid"],
                                 "properties": {**props, "layer": "backprojected_centroid"}})
    return {"type": "FeatureCollection", "features": features}


@router.get("/cases/compare/{a}/{b}")
async def compare_cases(a: str, b: str, user=Depends(get_current_user)):
    sides = {}
    for key, cid in (("a", a), ("b", b)):
        case = await _case(cid)
        spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "raw_input": 0})
        result = await _result(cid, None)
        cands = [{k: v for k, v in c.items() if k != "track"} for c in (result or {}).get("candidates", [])]
        sides[key] = {"case": case, "geojson": _geojson(case, spill, result), "candidates": cands, "version": (result or {}).get("version", 0)}
    ma = {c["mmsi"]: c for c in sides["a"]["candidates"]}
    mb = {c["mmsi"]: c for c in sides["b"]["candidates"]}
    shared = [{"mmsi": m, "vessel_name": ma[m].get("vessel_name") or mb[m].get("vessel_name"), "vessel_type": ma[m].get("vessel_type"),
               "a": {"rank": ma[m]["rank"], "score": ma[m]["score"], "status": ma[m]["status"]}, "b": {"rank": mb[m]["rank"], "score": mb[m]["score"], "status": mb[m]["status"]}}
              for m in ma if m in mb]
    shared.sort(key=lambda s: s["a"]["rank"] + s["b"]["rank"])
    return clean({**sides, "shared_vessels": shared, "disclaimer": "Repeat appearance across cases is an investigative lead, not evidence of responsibility."})


@router.get("/cases/{case_id}/geojson")
async def case_geojson(case_id: str, version: Optional[int] = None, user=Depends(get_current_user)):
    case = await _case(case_id)
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "raw_input": 0})
    result = await _result(case_id, version)
    return clean(_geojson(case, spill, result))


async def _bundle(case_id, version):
    case = await _case(case_id)
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0})
    scene = await db.scenes.find_one({"id": case["scene_id"]}, {"_id": 0}) if case.get("scene_id") else None
    result = await _result(case_id, version)
    all_versions = await db.correlation_results.find({"case_id": case_id}, {"_id": 0, "candidates": 0, "processing_log": 0}).sort("version", 1).to_list(100)
    reviews = await db.reviews.find({"case_id": case_id}, {"_id": 0}).sort("created_at", 1).to_list(500)
    entity_ids = [case_id, spill["id"]] + ([scene["id"]] if scene else [])
    audit_events = await db.audit_events.find({"entity_id": {"$in": entity_ids}}, {"_id": 0}).sort("created_at", 1).to_list(2000)
    jobs = await db.jobs.find({"payload.case_id": case_id}, {"_id": 0}).sort("created_at", 1).to_list(200)
    attachments = await db.attachments.find({"case_id": case_id, "is_deleted": False}, {"_id": 0}).sort("created_at", 1).to_list(200)
    feedback = await db.detector_feedback.find({"case_id": case_id}, {"_id": 0}).sort("created_at", 1).to_list(100)
    calculations = None
    if result:
        calculations = {"algorithm_version": result["algorithm_version"], "input_hash": result["input_hash"], "params": result["params"],
                        "environment": result["environment"], "degraded": result["degraded"], "spill_axis_bearing": result["spill_axis_bearing"],
                        "candidates": [{k: v for k, v in c.items() if k != "track"} for c in result["candidates"]],
                        "processing_log": result["processing_log"]}
    return clean({
        "case": case,
        "source_references": {"scene": scene, "spill_observation": spill, "storage_ref": scene.get("storage_ref") if scene else None,
                              "ais_fix_ids": [fid for c in (result or {}).get("candidates", []) for fid in c["evidence"]["fix_ids"]]},
        "geometries": _geojson(case, {k: v for k, v in spill.items() if k != "raw_input"}, result),
        "calculations": calculations,
        "result_versions": all_versions,
        "reviews": reviews,
        "audit_history": audit_events,
        "jobs": jobs,
        "attachments": attachments,
        "detector_feedback": feedback,
        "disclaimer": "Decision-support evidence bundle. Correlation output indicates possible/probable association only; responsibility requires analyst confirmation and corroborating evidence.",
    })


@router.get("/cases/{case_id}/evidence")
async def case_evidence(case_id: str, version: Optional[int] = None, user=Depends(get_current_user)):
    return await _bundle(case_id, version)


@router.get("/cases/{case_id}/evidence.pdf")
async def case_evidence_pdf(case_id: str, version: Optional[int] = None, user=Depends(get_current_user)):
    bundle = await _bundle(case_id, version)
    bundle["generated_by"] = f"{user.get('name')} <{user['email']}> ({user['role']})"
    images = []
    for att in bundle["attachments"]:
        if att.get("is_image") and len(images) < 6:
            try:
                data, _ = await get_object(att["storage_path"])
                images.append({"caption": att.get("caption") or att["original_filename"], "meta": f"{att['kind']} · {att['original_filename']} · uploaded by {att['uploaded_by']}", "bytes": data})
            except Exception as e:  # noqa: BLE001
                images.append({"caption": att.get("caption") or att["original_filename"], "meta": f"fetch failed: {str(e)[:80]}", "bytes": None})
    bundle["attachment_images"] = images
    try:
        from playbook import playbook_for_case
        bundle["playbook"] = await playbook_for_case(case_id)
    except Exception:  # noqa: BLE001
        bundle["playbook"] = None
    try:
        from vulnerability import assess
        bundle["vulnerability"] = await assess(case_id)
    except Exception:  # noqa: BLE001
        bundle["vulnerability"] = None
    pdf = build_pdf(bundle)
    await audit("case", case_id, "evidence.exported", {"format": "pdf", "version": bundle["case"].get("latest_result_version"), "bytes": len(pdf)}, user["email"])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{bundle["case"]["case_number"]}-evidence.pdf"'})
