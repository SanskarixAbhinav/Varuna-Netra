import asyncio
import logging

import httpx

logger = logging.getLogger("satellite")
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTIONS = {
    "sentinel-1-grd": {"label": "Sentinel-1 GRD (SAR, C-band)", "provider": "sentinel-1", "kind": "sar"},
    "sentinel-2-l2a": {"label": "Sentinel-2 L2A (optical)", "provider": "sentinel-2", "kind": "optical"},
}


def _normalize(item: dict) -> dict:
    p, a = item["properties"], item.get("assets", {})
    col = item.get("collection")
    return {
        "stac_id": item["id"], "collection": col, "provider": COLLECTIONS.get(col, {}).get("provider", col), "kind": COLLECTIONS.get(col, {}).get("kind"),
        "datetime": p.get("datetime"), "platform": p.get("platform"), "instrument_mode": p.get("sar:instrument_mode"),
        "polarizations": p.get("sar:polarizations"), "orbit_state": p.get("sat:orbit_state"), "relative_orbit": p.get("sat:relative_orbit"),
        "cloud_cover": p.get("eo:cloud_cover"), "product_type": p.get("sar:product_type") or p.get("s2:product_type"),
        "footprint": item.get("geometry"), "bbox": item.get("bbox"),
        "preview_href": (a.get("rendered_preview") or {}).get("href"), "thumbnail_href": (a.get("thumbnail") or {}).get("href"),
        "sar_assets": sorted(k for k in a if k in ("vv", "vh", "hh", "hv")),
        "stac_href": f"{STAC}/collections/{col}/items/{item['id']}",
    }


async def search_scenes(bbox: list | None, start: str, end: str, collection: str = "sentinel-1-grd", limit: int = 25, max_cloud: int | None = None, intersects: dict | None = None) -> dict:
    body = {"collections": [collection], "datetime": f"{start}/{end}", "limit": min(limit, 100), "sortby": [{"field": "datetime", "direction": "desc"}]}
    if intersects:
        body["intersects"] = intersects
    else:
        body["bbox"] = bbox
    if collection == "sentinel-2-l2a" and max_cloud is not None:
        body["query"] = {"eo:cloud_cover": {"lt": max_cloud}}
    logger.info("STAC search %s %s %s", collection, "intersects" if intersects else f"bbox={bbox}", body["datetime"])
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{STAC}/search", json=body)
        r.raise_for_status()
        d = r.json()
    feats = d.get("features", [])
    logger.info("STAC search %s → %d scene(s)", collection, len(feats))
    return {"count": len(feats), "matched": (d.get("context") or {}).get("matched"), "scenes": [_normalize(f) for f in feats], "source": "Microsoft Planetary Computer STAC (open, no key)"}


async def get_item_raw(collection: str, stac_id: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{STAC}/collections/{collection}/items/{stac_id}")
        r.raise_for_status()
        return r.json()


async def get_item(collection: str, stac_id: str) -> dict:
    return _normalize(await get_item_raw(collection, stac_id))


_preview_cache: dict = {}


async def fetch_preview(href: str, fallback: str | None = None) -> tuple[bytes, str]:
    if href in _preview_cache:
        return _preview_cache[href]
    last = None
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        for url in [href, href, fallback]:
            if not url:
                continue
            try:
                r = await c.get(url)
                r.raise_for_status()
                out = (r.content, r.headers.get("content-type", "image/png"))
                if len(_preview_cache) > 300:
                    _preview_cache.clear()
                _preview_cache[href] = out
                return out
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(1.5)
    raise last
