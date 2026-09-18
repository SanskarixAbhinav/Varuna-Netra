from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from auth import get_current_user, require_role
from db import db, clean, audit
from models import SceneCreate
from satellite import COLLECTIONS, STAC, search_scenes, get_item, fetch_preview
from services import create_scene

router = APIRouter()


class SceneSearch(BaseModel):
    bbox: List[float] = Field(min_length=4, max_length=4)
    start: str
    end: str
    collection: str = "sentinel-1-grd"
    limit: int = Field(default=25, ge=1, le=100)
    max_cloud: Optional[int] = Field(default=None, ge=0, le=100)


@router.get("/satellite/collections")
async def collections(user=Depends(get_current_user)):
    return {"collections": [{"id": k, **v} for k, v in COLLECTIONS.items()], "stac": STAC,
            "basemaps": [{"id": "VIIRS_SNPP_CorrectedReflectance_TrueColor", "label": "VIIRS SNPP true colour (daily)", "matrix": "GoogleMapsCompatible_Level9", "ext": "jpg"},
                         {"id": "MODIS_Terra_CorrectedReflectance_TrueColor", "label": "MODIS Terra true colour (daily)", "matrix": "GoogleMapsCompatible_Level9", "ext": "jpg"},
                         {"id": "VIIRS_SNPP_DayNightBand_ENCC", "label": "VIIRS day/night band (night lights)", "matrix": "GoogleMapsCompatible_Level8", "ext": "png"}],
            "gibs_template": "https://gibs-{s}.earthdata.nasa.gov/wmts/epsg3857/best/{layer}/default/{time}/{matrix}/{z}/{y}/{x}.{ext}"}


@router.post("/satellite/search")
async def search(body: SceneSearch, user=Depends(get_current_user)):
    if body.collection not in COLLECTIONS:
        raise HTTPException(400, f"collection must be one of {list(COLLECTIONS)}")
    w, s, e, n = body.bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HTTPException(400, "bbox must be [west, south, east, north]")
    if (e - w) * (n - s) > 3600:
        raise HTTPException(400, "search area too large — zoom in (keep the box under ~60°×60°)")
    try:
        res = await search_scenes(body.bbox, body.start, body.end, body.collection, body.limit, body.max_cloud)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"STAC search failed: {str(ex)[:200]}")
    registered = {s["provider_scene_id"] for s in await db.scenes.find({"provider_scene_id": {"$in": [x["stac_id"] for x in res["scenes"]]}}, {"provider_scene_id": 1, "id": 1}).to_list(200)}
    ids = {s["provider_scene_id"]: s["id"] for s in await db.scenes.find({"provider_scene_id": {"$in": list(registered)}}, {"provider_scene_id": 1, "id": 1}).to_list(200)}
    for sc in res["scenes"]:
        sc["registered_scene_id"] = ids.get(sc["stac_id"])
    await audit("satellite", body.collection, "satellite.searched", {"bbox": body.bbox, "start": body.start, "end": body.end, "count": res["count"]}, user["email"])
    return res


class RegisterRequest(BaseModel):
    collection: str
    stac_id: str
    detect: bool = False


@router.post("/satellite/register", status_code=201)
async def register(body: RegisterRequest, user=Depends(require_role("analyst"))):
    if body.collection not in COLLECTIONS:
        raise HTTPException(400, "unknown collection")
    existing = await db.scenes.find_one({"provider_scene_id": body.stac_id}, {"_id": 0})
    if existing:
        return clean({"scene": existing, "already_registered": True})
    try:
        it = await get_item(body.collection, body.stac_id)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"STAC item fetch failed: {str(ex)[:200]}")
    payload = SceneCreate(provider=it["provider"], provider_scene_id=it["stac_id"], sensor_mode=it.get("instrument_mode") or it.get("product_type"),
                          polarization="+".join(it["polarizations"]) if it.get("polarizations") else None, acquisition_time=it["datetime"],
                          footprint=it["footprint"], storage_ref=it["stac_href"],
                          metadata={"stac_collection": it["collection"], "platform": it.get("platform"), "orbit_state": it.get("orbit_state"), "relative_orbit": it.get("relative_orbit"), "bbox": it.get("bbox"),
                                    "cloud_cover": it.get("cloud_cover"), "preview_href": it.get("preview_href"), "thumbnail_href": it.get("thumbnail_href"), "source": "Microsoft Planetary Computer STAC"})
    try:
        scene = await create_scene(payload, user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    out = {"scene": scene, "already_registered": False}
    if body.detect:
        from detector import detect_scene
        try:
            det = await detect_scene(scene, user["email"])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"detector failed: {str(e)[:200]}")
        first = det["cases"][0] if det.get("cases") else None
        case = await db.cases.find_one({"id": first["case_id"]}, {"_id": 0}) if first else None
        out.update({"detection": det, "case": case, "detector_note": det["note"]})
    return clean(out)


async def _scene_by_any_id(scene_id: str) -> dict:
    scene = await db.scenes.find_one({"$or": [{"id": scene_id}, {"provider_scene_id": scene_id}]}, {"_id": 0})
    if not scene:
        raise HTTPException(404, "Sentinel scene not registered — select it in Scene Explorer first")
    return scene


@router.get("/sentinel/latest")
async def sentinel_latest(user=Depends(get_current_user)):
    from sentinel_assets import scene_status_summary
    scene = await db.scenes.find_one({"metadata.stac_collection": "sentinel-1-grd"}, {"_id": 0}, sort=[("acquisition_time", -1)])
    if not scene:
        return {"scene": None, "note": "No Sentinel-1 scene registered yet"}
    return clean({"scene": scene, "scene_status": scene_status_summary(scene, {"scene_id": scene["id"]}), "note": "Latest registered Sentinel-1 acquisition (near-real-time archive, not a live feed)."})


@router.get("/sentinel/nearest")
async def sentinel_nearest(lon: float, lat: float, target: str, max_hours: int = Query(168, ge=1, le=720), buffer_deg: float = Query(0.05, gt=0, le=2), user=Depends(get_current_user)):
    """Nearest real Sentinel-1 GRD acquisition to (lon, lat, target time), adaptive ±36 h → ±7 d. Works for any maritime AOI worldwide."""
    from sentinel_assets import nearest_scenes, SceneAssetError
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise HTTPException(400, "lon must be within ±180 and lat within ±90 — check for reversed coordinates")
    try:
        t = datetime.fromisoformat(target.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "target must be an ISO-8601 timestamp")
    spill = {"geometry": {"type": "Polygon", "coordinates": [[[lon - buffer_deg, lat - buffer_deg], [lon + buffer_deg, lat - buffer_deg], [lon + buffer_deg, lat + buffer_deg], [lon - buffer_deg, lat + buffer_deg], [lon - buffer_deg, lat - buffer_deg]]]}}
    try:
        res = await nearest_scenes(spill, t if t.tzinfo else t.replace(tzinfo=timezone.utc), max_hours)
    except SceneAssetError as e:
        raise HTTPException(400, str(e))
    if res["found"]:
        best = {k: v for k, v in res["candidates"][0].items() if k != "_item"}
        return clean({"found": True, "state": res["state"], "scene": best, "time_difference_hours": best["time_difference_hours"], "search_window_hours": res["search_window_hours"], "stages_tried": res["stages_tried"],
                      "candidates": len(res["candidates"]), "source": "Microsoft Planetary Computer STAC (real archive, not live)"})
    return clean({"found": False, "state": res["state"], "reason": res["reason"], "stages_tried": res["stages_tried"]})


@router.get("/sentinel/search")
async def sentinel_search_get(bbox: str, start: str, end: str, collection: str = "sentinel-1-grd", limit: int = 25, user=Depends(get_current_user)):
    try:
        parts = [float(x) for x in bbox.split(",")]
        assert len(parts) == 4
    except (ValueError, AssertionError):
        raise HTTPException(400, "bbox must be west,south,east,north")
    return await search(SceneSearch(bbox=parts, start=start, end=end, collection=collection, limit=max(1, min(limit, 100))), user)


@router.get("/sentinel/scenes/{scene_id}")
async def sentinel_scene(scene_id: str, check_access: bool = False, user=Depends(get_current_user)):
    from sentinel_assets import resolve_scene_assets
    scene = await _scene_by_any_id(scene_id)
    st = await resolve_scene_assets(scene, check_access=check_access)
    return clean({**st, "footprint": scene["footprint"], "status": scene.get("status"), "detector_summary": scene.get("detector_summary")})


@router.get("/sentinel/scenes/{scene_id}/assets")
async def sentinel_scene_assets(scene_id: str, user=Depends(get_current_user)):
    from sentinel_assets import resolve_scene_assets
    scene = await _scene_by_any_id(scene_id)
    st = await resolve_scene_assets(scene, check_access=True)
    return clean({"scene_id": scene["id"], "provider_scene_id": scene["provider_scene_id"], "collection": st["collection"], "assets": (scene.get("assets") or {}).get("assets", []),
                  "analysis_asset": st["analysis_asset"], "sar_assets": st["sar_assets"], "sar_asset_accessible": st.get("sar_asset_accessible"), "state": st["state"], "reason": st["reason"],
                  "signing": "Planetary Computer SAS token resolved server-side just-in-time; signed URLs are never persisted or returned."})


@router.get("/sentinel/scenes/{scene_id}/quicklook")
async def sentinel_scene_quicklook(scene_id: str, user=Depends(get_current_user)):
    from detector import get_quicklook
    scene = await _scene_by_any_id(scene_id)
    try:
        png = await get_quicklook(scene)
    except ValueError as e:
        raise HTTPException(502, str(e)[:200])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Quicklook generation failed: {str(e)[:200]}")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/satellite/preview")
async def preview(collection: str, stac_id: str, user=Depends(get_current_user)):
    if collection not in COLLECTIONS:
        raise HTTPException(400, "unknown collection")
    scene = await db.scenes.find_one({"provider_scene_id": stac_id}, {"_id": 0, "metadata": 1})
    md = (scene or {}).get("metadata", {})
    href, thumb = md.get("preview_href"), md.get("thumbnail_href")
    if not href:
        try:
            it = await get_item(collection, stac_id)
            href, thumb = it.get("preview_href"), it.get("thumbnail_href")
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, f"STAC item fetch failed: {str(ex)[:200]}")
    if not href:
        raise HTTPException(404, "no preview available for this scene")
    try:
        data, ct = await fetch_preview(href, thumb)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"preview fetch failed: {str(ex)[:200]}")
    return Response(content=data, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
