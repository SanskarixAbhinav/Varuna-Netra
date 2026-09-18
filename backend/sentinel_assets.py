"""Sentinel-1 asset resolution: QUICKLOOK (visualization) vs SAR ASSET (analysis input).
Stable identifiers only are persisted (scene_id, collection, asset_key); signed URLs are resolved just-in-time and never stored."""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from db import db, audit, to_utc
from satellite import get_item_raw, search_scenes

logger = logging.getLogger("sentinel_assets")
DATA_API = "https://planetarycomputer.microsoft.com/api/data/v1"
SAS_API = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
RESCALE = {"vv": "0,600", "vh": "0,270", "hh": "0,600", "hv": "0,270"}
_sas_cache: dict = {}


class SceneAssetError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def summarize_assets(item: dict) -> dict:
    """Inspect the real STAC asset dictionary instead of assuming key names."""
    assets = item.get("assets") or {}
    listing = [{"key": k, "type": (v or {}).get("type"), "roles": (v or {}).get("roles") or []} for k, v in assets.items()]
    sar = [a["key"] for a in listing if "data" in a["roles"] and "tiff" in str(a["type"]).lower()]
    analysis = next((k for k in ("vv", "hh", "vh", "hv") if k in sar), sar[0] if sar else None)
    preview = next((k for k in ("rendered_preview", "thumbnail") if k in assets), None)
    return {"available_assets": [a["key"] for a in listing], "assets": listing, "sar_assets": sar, "analysis_asset": analysis,
            "native_preview_asset": preview, "preview_href": (assets.get("rendered_preview") or {}).get("href"), "thumbnail_href": (assets.get("thumbnail") or {}).get("href")}


async def sas_token(collection: str) -> str:
    hit = _sas_cache.get(collection)
    if hit and hit[0] - time.time() > 300:
        return hit[1]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{SAS_API}/{collection}")
        r.raise_for_status()
        d = r.json()
    exp = datetime.fromisoformat(d["msft:expiry"].replace("Z", "+00:00")).timestamp()
    _sas_cache[collection] = (exp, d["token"])
    return d["token"]


async def signed_href(collection: str, href: str) -> str:
    """Planetary Computer blob assets need a short-lived SAS token; regenerated on demand (never persisted)."""
    if "blob.core.windows.net" not in href:
        return href
    return f"{href}?{await sas_token(collection)}"


async def sar_asset_accessible(collection: str, href: str) -> tuple[bool, Optional[str]]:
    try:
        url = await signed_href(collection, href)
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.head(url)
            if r.status_code == 403:  # expired/invalid token → regenerate once
                _sas_cache.pop(collection, None)
                r = await c.head(await signed_href(collection, href))
        return r.status_code == 200, None if r.status_code == 200 else f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:160]


async def sar_bbox_png(collection: str, stac_id: str, asset: str, bbox_wsen: list, size: int = 1024) -> bytes:
    """Render an AOI window of the REAL SAR raster (COG) through the PC data API — analysis input, not a thumbnail."""
    w, s, e, n = bbox_wsen
    url = f"{DATA_API}/item/bbox/{w},{s},{e},{n}/{size}x{size}.png"
    params = {"collection": collection, "item": stac_id, "assets": asset, "rescale": RESCALE.get(asset, "0,600")}
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.get(url, params=params)
    if r.status_code != 200:
        raise SceneAssetError("raster_download_failed", f"Raster download failed for {asset} window: HTTP {r.status_code}")
    return r.content


def _stac_collection(scene: dict) -> Optional[str]:
    return (scene.get("metadata") or {}).get("stac_collection")


async def resolve_scene_assets(scene: dict, check_access: bool = False) -> dict:
    """Authoritative scene status. Caches the stable asset inventory on the scene document."""
    md = scene.get("metadata") or {}
    col = _stac_collection(scene)
    base = {"scene_id": scene["id"], "provider_scene_id": scene["provider_scene_id"], "collection": col, "provider": scene["provider"], "acquisition_time": scene["acquisition_time"],
            "bbox": md.get("bbox"), "quicklook_available": bool(scene.get("quicklook_path") or md.get("preview_href") or md.get("thumbnail_href")),
            "quicklook_kind": scene.get("quicklook_kind") or ("native" if md.get("preview_href") or md.get("thumbnail_href") else None),
            "quicklook_status": scene.get("quicklook_status") or ("ready" if scene.get("quicklook_path") else ("available" if md.get("preview_href") else "none")),
            "real_data": bool(col), "provider_source": md.get("source")}
    if not col:
        return {**base, "available_assets": [], "sar_assets": [], "analysis_asset": None, "sar_available": False, "state": "SAR_UNAVAILABLE",
                "reason": "SAR asset missing — scene was registered manually without a Sentinel STAC item"}
    inv = scene.get("assets")
    if not inv:
        try:
            inv = summarize_assets(await get_item_raw(col, scene["provider_scene_id"]))
        except Exception as e:  # noqa: BLE001
            return {**base, "available_assets": [], "sar_assets": [], "analysis_asset": None, "sar_available": False, "state": "SAR_UNAVAILABLE",
                    "reason": f"Sentinel scene metadata unavailable: {str(e)[:120]}"}
        await db.scenes.update_one({"id": scene["id"]}, {"$set": {"assets": inv, "metadata.preview_href": md.get("preview_href") or inv["preview_href"], "metadata.thumbnail_href": md.get("thumbnail_href") or inv["thumbnail_href"]}})
        scene["assets"] = inv
    out = {**base, "available_assets": inv["available_assets"], "sar_assets": inv["sar_assets"], "analysis_asset": inv["analysis_asset"], "sar_available": bool(inv["analysis_asset"]),
           "native_preview_asset": inv.get("native_preview_asset"), "quicklook_available": base["quicklook_available"] or bool(inv.get("preview_href"))}
    if not out["sar_available"]:
        out.update({"state": "SAR_UNAVAILABLE", "reason": f"SAR asset missing — STAC item exposes no GeoTIFF data asset (found {inv['available_assets']})"})
    elif out["quicklook_status"] == "generating":
        out.update({"state": "QUICKLOOK_GENERATING", "reason": "SAR available; preview still being generated"})
    else:
        out.update({"state": "SAR_READY", "reason": None})
    if check_access and out["sar_available"]:
        try:
            item = await get_item_raw(col, scene["provider_scene_id"])
            ok, err = await sar_asset_accessible(col, item["assets"][out["analysis_asset"]]["href"])
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)[:160]
        out["sar_asset_accessible"] = ok
        if not ok:
            out.update({"state": "SAR_UNAVAILABLE", "reason": f"Asset signing/access failed: {err}"})
    return out


async def generate_quicklook(scene: dict) -> bytes:
    """No native preview → render the whole footprint from the real SAR raster (visualization only)."""
    st = await resolve_scene_assets(scene)
    if not st["sar_available"]:
        raise SceneAssetError("sar_missing", st["reason"])
    await db.scenes.update_one({"id": scene["id"]}, {"$set": {"quicklook_status": "generating"}})
    try:
        from shapely.geometry import shape
        bbox = st["bbox"] or list(shape(scene["footprint"]).bounds)
        png = await sar_bbox_png(st["collection"], scene["provider_scene_id"], st["analysis_asset"], bbox)
    except Exception as e:
        await db.scenes.update_one({"id": scene["id"]}, {"$set": {"quicklook_status": "failed", "quicklook_error": str(e)[:200]}})
        raise SceneAssetError("quicklook_generation_failed", f"Quicklook generation failed: {str(e)[:160]}")
    await db.scenes.update_one({"id": scene["id"]}, {"$set": {"quicklook_status": "ready", "quicklook_kind": "generated", "quicklook_error": None}})
    return png


def scene_status_summary(scene: Optional[dict], case: dict) -> dict:
    """Cheap, network-free summary for GET /cases/{id}; derived from persisted scene state only. Distinct failure states, never one generic warning."""
    att = case.get("scene_attachment") or {}
    last = case.get("scene_search") or {}
    if not case.get("scene_id"):
        if last.get("state") in ("NO_COVERAGE_7D", "STAC_FAILED", "NEAREST_FOUND_EXTENDED"):
            return {"scene_id": None, "state": last["state"], "sar_available": False, "quicklook_available": False, "analysis_asset": None, "reason": last.get("reason"), "sar_confirmation": "PENDING", "search": last}
        return {"scene_id": None, "state": "NO_SCENE_SELECTED", "sar_available": False, "quicklook_available": False, "analysis_asset": None, "reason": "No Sentinel-1 scene attached yet — run the acquisition search", "sar_confirmation": "PENDING", "search": last or None}
    if not scene:
        return {"scene_id": case["scene_id"], "state": "ATTACH_FAILED", "sar_available": False, "quicklook_available": False, "analysis_asset": None, "reason": "Sentinel scene metadata unavailable (scene record missing)", "sar_confirmation": "PENDING"}
    md, inv = scene.get("metadata") or {}, scene.get("assets") or {}
    sar = bool(_stac_collection(scene)) and (inv.get("analysis_asset") is not None if inv else True)
    ql = bool(scene.get("quicklook_path") or md.get("preview_href") or md.get("thumbnail_href"))
    state = "SAR_ASSET_UNAVAILABLE" if not sar else ("QUICKLOOK_GENERATING" if scene.get("quicklook_status") == "generating" else "SAR_READY")
    return {"scene_id": scene["id"], "provider_scene_id": scene["provider_scene_id"], "collection": _stac_collection(scene), "acquisition_time": scene["acquisition_time"], "platform": md.get("platform"), "polarization": scene.get("polarization"),
            "sar_available": sar, "quicklook_available": ql, "quicklook_kind": scene.get("quicklook_kind") or ("native" if ql else None), "quicklook_status": scene.get("quicklook_status") or ("ready" if scene.get("quicklook_path") else "available" if ql else "none"),
            "analysis_asset": inv.get("analysis_asset") or ("vv" if sar else None), "available_assets": inv.get("available_assets"), "state": state, "sar_confirmation": "READY" if sar else "PENDING",
            "time_difference_hours": att.get("time_difference_hours"), "overlap_percent": att.get("overlap_percent"), "search_window_hours": att.get("search_window_hours"), "auto": att.get("auto"), "attached_at": att.get("attached_at"),
            "reason": None if sar else "SAR asset missing — scene was registered manually without Sentinel-1 STAC imagery"}


STAGES_H = (36, 72, 120, 168)
FMT = "%Y-%m-%dT%H:%M:%SZ"


def _spill_aoi(spill: dict, buffer_deg: float = 0.05) -> tuple[dict, object]:
    """Real spill polygon (repaired) buffered by a small margin — never a fixed point. Validates lon/lat order & range."""
    from shapely.geometry import shape, mapping
    from shapely.validation import make_valid
    g = make_valid(shape(spill["geometry"])).buffer(buffer_deg)
    minx, miny, maxx, maxy = g.bounds
    if not (-180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90):
        raise SceneAssetError("invalid_aoi", f"spill geometry out of range (lon {minx}..{maxx}, lat {miny}..{maxy}) — coordinates must be [lon, lat]")
    return mapping(g), g


def rank_scenes(scenes: list, aoi_geom, t: datetime, window_h: int) -> list:
    from shapely.geometry import shape
    out = []
    for s in scenes:
        dt = datetime.fromisoformat(s["datetime"].replace("Z", "+00:00"))
        diff_h = abs((dt - t).total_seconds()) / 3600
        try:
            fp = shape(s["footprint"]) if s.get("footprint") else None
            overlap = (fp.intersection(aoi_geom).area / aoi_geom.area * 100) if fp is not None and aoi_geom.area else None
        except Exception:  # noqa: BLE001
            overlap = None
        sar = bool(s.get("sar_assets"))
        score = 0.5 * ((overlap or 0) / 100) + 0.4 * max(0.0, 1 - diff_h / max(window_h, 1)) + 0.1 * (1 if sar else 0)
        out.append({"scene_id": s["stac_id"], "collection": s["collection"], "acquisition_time": s["datetime"], "time_difference_hours": round(diff_h, 2), "signed_offset_hours": round((dt - t).total_seconds() / 3600, 2),
                    "overlap_percent": round(overlap, 1) if overlap is not None else None, "sar_available": sar, "sar_assets": s.get("sar_assets"), "polarization": s.get("polarizations"),
                    "platform": s.get("platform"), "orbit_state": s.get("orbit_state"), "relative_orbit": s.get("relative_orbit"), "instrument_mode": s.get("instrument_mode"), "footprint": s.get("footprint"), "bbox": s.get("bbox"),
                    "preview": f"/satellite/preview?collection={s['collection']}&stac_id={s['stac_id']}" if s.get("preview_href") or s.get("thumbnail_href") else None, "score": round(score, 3), "_item": s})
    out.sort(key=lambda r: -r["score"])
    return out


async def nearest_scenes(spill: dict, t: datetime, max_hours: int = 168, collection: str = "sentinel-1-grd") -> dict:
    """Adaptive staged search (±36 h → ±72 h → ±5 d → ±7 d) with STAC intersects on the real spill AOI. Returns the stage that produced results."""
    aoi, aoi_geom = _spill_aoi(spill)
    stages_tried = []
    for w in [s for s in STAGES_H if s <= max_hours] or [max_hours]:
        try:
            res = await search_scenes(None, (t - timedelta(hours=w)).strftime(FMT), (t + timedelta(hours=w)).strftime(FMT), collection, 50, intersects=aoi)
        except Exception as e:  # noqa: BLE001
            return {"found": False, "state": "STAC_FAILED", "reason": f"STAC search failed: {str(e)[:140]}", "stages_tried": stages_tried, "aoi": aoi, "target_time": t}
        stages_tried.append({"window_hours": w, "scenes": res["count"]})
        if res["scenes"]:
            ranked = rank_scenes(res["scenes"], aoi_geom, t, w)
            state = "FOUND_36H" if w == STAGES_H[0] else "NEAREST_FOUND_EXTENDED"
            return {"found": True, "state": state, "search_window_hours": w, "stages_tried": stages_tried, "candidates": ranked, "aoi": aoi, "target_time": t, "collection": collection,
                    "reason": None if w == STAGES_H[0] else f"No acquisition within ±{STAGES_H[0]} h; nearest found in the ±{w} h window"}
    return {"found": False, "state": "NO_COVERAGE_7D", "reason": f"No Sentinel-1 acquisition intersects this AOI within ±{max_hours // 24} days of {t.strftime(FMT)} — satellite revisit gap. AIS/jurisdiction investigation can continue (SAR confirmation pending).",
            "stages_tried": stages_tried, "aoi": aoi, "target_time": t, "collection": collection}


async def register_stac_item(it: dict, actor: str) -> dict:
    from services import create_scene
    from models import SceneCreate
    existing = await db.scenes.find_one({"provider_scene_id": it["stac_id"]}, {"_id": 0})
    if existing:
        return existing
    payload = SceneCreate(provider=it["provider"], provider_scene_id=it["stac_id"], sensor_mode=it.get("instrument_mode"), polarization="+".join(it["polarizations"]) if it.get("polarizations") else None,
                          acquisition_time=it["datetime"], footprint=it["footprint"], storage_ref=it["stac_href"],
                          metadata={"stac_collection": it["collection"], "platform": it.get("platform"), "orbit_state": it.get("orbit_state"), "relative_orbit": it.get("relative_orbit"), "bbox": it.get("bbox"),
                                    "preview_href": it.get("preview_href"), "thumbnail_href": it.get("thumbnail_href"), "sar_assets": it.get("sar_assets"), "source": "Microsoft Planetary Computer STAC"})
    return await create_scene(payload, actor)


async def _remember_search(case_id: str, res: dict):
    await db.cases.update_one({"id": case_id}, {"$set": {"scene_search": {"state": res["state"], "reason": res.get("reason"), "stages_tried": res.get("stages_tried"), "search_window_hours": res.get("search_window_hours"),
                                                                         "candidates": [{k: v for k, v in c.items() if k not in ("_item", "footprint")} for c in res.get("candidates", [])[:10]], "at": datetime.now(timezone.utc)}}})


async def auto_attach_scene(case: dict, actor: str, max_hours: int = 168) -> dict:
    """Adaptive search on the real spill AOI; attaches the best-ranked real Sentinel-1 GRD scene. Raises a state-specific SceneAssetError otherwise."""
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "geometry": 1, "centroid": 1, "acquisition_time": 1})
    t = to_utc(spill["acquisition_time"])
    res = await nearest_scenes(spill, t, max_hours)
    await _remember_search(case["id"], res)
    if not res["found"]:
        raise SceneAssetError(res["state"].lower(), res["reason"])
    best = res["candidates"][0]
    scene = await register_stac_item(best["_item"], actor)
    await attach_scene(case, scene, actor, auto=True, rank=best, window_h=res["search_window_hours"])
    return scene


async def attach_scene(case: dict, scene: dict, actor: str, auto: bool = False, rank: Optional[dict] = None, window_h: Optional[int] = None) -> None:
    now = datetime.now(timezone.utc)
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "geometry": 1, "acquisition_time": 1})
    t = to_utc(spill["acquisition_time"])
    if rank is None:  # manual selection: compute the same truthful metrics from persisted scene metadata
        _, aoi_geom = _spill_aoi(spill)
        rank = rank_scenes([{"stac_id": scene["provider_scene_id"], "collection": _stac_collection(scene), "datetime": to_utc(scene["acquisition_time"]).strftime(FMT), "footprint": scene.get("footprint"),
                             "sar_assets": (scene.get("metadata") or {}).get("sar_assets") or (["vv"] if _stac_collection(scene) else []), "polarizations": (scene.get("polarization") or "").split("+") if scene.get("polarization") else None,
                             "platform": (scene.get("metadata") or {}).get("platform"), "bbox": (scene.get("metadata") or {}).get("bbox")}], aoi_geom, t, window_h or STAGES_H[0])[0]
    md = scene.get("metadata") or {}
    attachment = {"case_id": case["id"], "spill_id": case["spill_observation_id"], "scene_id": scene["id"], "provider_scene_id": scene["provider_scene_id"], "collection": _stac_collection(scene), "provider": scene.get("provider"),
                  "acquisition_time": scene["acquisition_time"], "scene_bbox": md.get("bbox"), "scene_geometry": scene.get("footprint"), "analysis_asset_key": (scene.get("assets") or {}).get("analysis_asset") or ("vv" if _stac_collection(scene) else None),
                  "time_difference_hours": rank["time_difference_hours"], "signed_offset_hours": rank.get("signed_offset_hours"), "overlap_percent": rank.get("overlap_percent"), "search_window_hours": window_h, "score": rank.get("score"),
                  "attached_at": now, "attached_by": actor, "auto": auto}
    await db.cases.update_one({"id": case["id"]}, {"$set": {"scene_id": scene["id"], "scene_attachment": attachment}})
    await db.spill_observations.update_one({"id": case["spill_observation_id"]}, {"$set": {"scene_id": scene["id"]}})
    await audit("case", case["id"], "case.scene_attached", {"scene_id": scene["id"], "provider_scene_id": scene["provider_scene_id"], "auto": auto, "time_difference_hours": rank["time_difference_hours"]}, actor)
