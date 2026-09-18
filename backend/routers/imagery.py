import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from auth import get_current_user, require_role
from db import db, clean, to_utc
from detector import detect_scene, get_quicklook
from satellite import search_scenes

router = APIRouter()
_density_cache: dict = {}


def _res_for_zoom(z: int) -> float:
    return max(0.02, 4.0 / (2 ** max(z - 2, 0)))


@router.get("/ais/density")
async def ais_density(hours: int = Query(24, ge=1, le=8760), zoom: int = Query(4, ge=0, le=14), bbox: Optional[str] = None, user=Depends(get_current_user)):
    """Server-side grid aggregation (hex-like square bins sized by zoom) — never streams raw pings."""
    key = (hours, zoom, bbox)
    hit = _density_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    res = _res_for_zoom(zoom)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    match = {"timestamp": {"$gte": since}}
    if bbox:
        try:
            w, s, e, n = [float(x) for x in bbox.split(",")]
            match["lon"] = {"$gte": w, "$lte": e}
            match["lat"] = {"$gte": s, "$lte": n}
        except ValueError:
            raise HTTPException(400, "bbox must be w,s,e,n")
    pipe = [{"$match": match},
            {"$group": {"_id": {"x": {"$floor": {"$divide": ["$lon", res]}}, "y": {"$floor": {"$divide": ["$lat", res]}}}, "n": {"$sum": 1}, "v": {"$addToSet": "$mmsi"}}},
            {"$project": {"_id": 0, "x": "$_id.x", "y": "$_id.y", "n": 1, "vessels": {"$size": "$v"}}}, {"$sort": {"n": -1}}, {"$limit": 6000}]
    cells = await db.ais_positions.aggregate(pipe).to_list(6000)
    mx = max((c["n"] for c in cells), default=1)
    out = {"hours": hours, "resolution_deg": res, "cells": [{"lat": round((c["y"] + 0.5) * res, 4), "lon": round((c["x"] + 0.5) * res, 4), "count": c["n"], "vessels": c["vessels"], "w": round(math.log1p(c["n"]) / math.log1p(mx), 3)} for c in cells],
           "max": mx, "since": since, "note": "Aggregated server-side into grid bins; raw positions never sent to the client."}
    out = clean(out)
    _density_cache[key] = (time.time(), out)
    return out


@router.get("/scenes/{scene_id}/quicklook")
async def scene_quicklook(scene_id: str, user=Depends(get_current_user)):
    scene = await db.scenes.find_one({"id": scene_id}, {"_id": 0})
    if not scene:
        raise HTTPException(404, "scene not found")
    md = scene.get("metadata") or {}
    if not (md.get("preview_href") or md.get("thumbnail_href") or md.get("stac_collection") or scene.get("quicklook_path")):
        raise HTTPException(404, "SAR asset missing — scene was registered manually without Sentinel STAC imagery, so no quicklook can be produced")
    try:
        png = await get_quicklook(scene)
    except ValueError as e:
        raise HTTPException(502, str(e)[:200])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Quicklook generation failed: {str(e)[:200]}")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/scenes/{scene_id}/overlay")
async def scene_overlay(scene_id: str, user=Depends(get_current_user)):
    scene = await db.scenes.find_one({"id": scene_id}, {"_id": 0})
    if not scene:
        raise HTTPException(404, "scene not found")
    md = scene.get("metadata") or {}
    from shapely.geometry import shape
    bbox = md.get("bbox") or list(shape(scene["footprint"]).bounds)
    return clean({"scene_id": scene_id, "has_quicklook": bool(md.get("preview_href") or md.get("thumbnail_href") or md.get("stac_collection") or scene.get("quicklook_path")), "bounds": [[bbox[1], bbox[0]], [bbox[3], bbox[2]]], "bbox": bbox, "footprint": scene["footprint"],
                  "provider_scene_id": scene["provider_scene_id"], "acquisition_time": scene["acquisition_time"], "detector": scene.get("detector_version"), "detector_summary": scene.get("detector_summary"),
                  "note": "Quicklook stretched to the scene bounding box (EPSG:4326, approximate georeferencing)."})


@router.post("/scenes/{scene_id}/detect-dark-spots", status_code=201)
async def detect_dark_spots(scene_id: str, user=Depends(require_role("analyst"))):
    scene = await db.scenes.find_one({"id": scene_id}, {"_id": 0})
    if not scene:
        raise HTTPException(404, "scene not found")
    try:
        return clean(await detect_scene(scene, user["email"]))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"detector failed: {str(e)[:200]}")


@router.get("/cases/{case_id}/before-after")
async def before_after(case_id: str, collection: str = "sentinel-1-grd", days: int = Query(30, ge=1, le=120), user=Depends(get_current_user)):
    case = await db.cases.find_one({"id": case_id}, {"_id": 0})
    if not case:
        raise HTTPException(404, "case not found")
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "geometry": 1, "centroid": 1})
    lon, lat = spill["centroid"]["coordinates"]
    pad = 0.15
    bbox = [round(lon - pad, 4), round(lat - pad, 4), round(lon + pad, 4), round(lat + pad, 4)]
    t = to_utc(case["acquisition_time"])
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        before = await search_scenes(bbox, (t - timedelta(days=days)).strftime(fmt), (t - timedelta(minutes=1)).strftime(fmt), collection, 5, 60)
        after = await search_scenes(bbox, (t + timedelta(minutes=1)).strftime(fmt), (t + timedelta(days=days)).strftime(fmt), collection, 50, 60)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"STAC search failed: {str(e)[:200]}")
    after_sorted = sorted(after["scenes"], key=lambda s: s["datetime"])
    return clean({"case_number": case["case_number"], "acquisition_time": t, "bbox": bbox, "spill_geometry": spill["geometry"], "collection": collection,
                  "before": before["scenes"][0] if before["scenes"] else None, "after": after_sorted[0] if after_sorted else None,
                  "before_candidates": before["scenes"][:5], "after_candidates": after_sorted[:5]})


@router.get("/spill-events")
async def spill_events(offset: int = Query(0, ge=0), limit: int = Query(24, ge=1, le=100), start: Optional[str] = None, end: Optional[str] = None, min_conf: Optional[float] = None,
                       status: Optional[str] = None, source: Optional[str] = None, jurisdiction: Optional[str] = None, user=Depends(get_current_user)):
    q = {}
    if start or end:
        q["acquisition_time"] = {}
        if start:
            q["acquisition_time"]["$gte"] = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if end:
            q["acquisition_time"]["$lte"] = datetime.fromisoformat(end.replace("Z", "+00:00"))
    if min_conf is not None:
        q["detection_confidence"] = {"$gte": min_conf}
    if status:
        q["attribution_status"] = status
    if source:
        q["source"] = source
    if jurisdiction:
        q["primary_jurisdiction.code"] = jurisdiction
    total = await db.cases.count_documents(q)
    proj = {"_id": 0, "id": 1, "case_number": 1, "acquisition_time": 1, "attribution_status": 1, "review_state": 1, "detection_confidence": 1, "source": 1, "primary_jurisdiction": 1,
            "centroid": 1, "confirmed_vessel_mmsi": 1, "candidate_count": 1, "top_score": 1, "thumbnail_attachment_id": 1, "scene_id": 1, "quality_flags": 1}
    rows = await db.cases.find(q, proj).sort("acquisition_time", -1).skip(offset).limit(limit).to_list(limit)
    scene_ids = [r["scene_id"] for r in rows if r.get("scene_id")]
    scenes = {s["id"]: s for s in await db.scenes.find({"id": {"$in": scene_ids}}, {"_id": 0, "id": 1, "provider_scene_id": 1, "metadata.preview_href": 1, "quicklook_path": 1}).to_list(500)}
    for r in rows:
        sc = scenes.get(r.get("scene_id"))
        r["scene_provider_id"] = sc["provider_scene_id"] if sc else None
        r["thumb"] = f"/attachments/{r['thumbnail_attachment_id']}/download" if r.get("thumbnail_attachment_id") else (f"/scenes/{r['scene_id']}/quicklook" if sc and ((sc.get("metadata") or {}).get("preview_href") or sc.get("quicklook_path")) else None)
    sources = await db.cases.distinct("source")
    return clean({"total": total, "offset": offset, "limit": limit, "events": rows, "sources": sources})
