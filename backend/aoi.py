"""Investigation AOI — the single geometry that drives Sentinel-1 search, AIS coverage and jurisdiction context. Location-agnostic; SIH Mumbai is just a preset."""
from datetime import datetime, timezone
from typing import Optional

from shapely.geometry import shape, box, mapping

import ais_live
from db import db
from geo import validate_polygon
from jurisdiction import lookup, zone_public

PRESETS = {
    "sih_mumbai": {"name": "SIH demo · Mumbai / Arabian Sea", "bbox": [70.5, 17.5, 73.5, 20.5], "note": "Smart India Hackathon demonstration preset — Mumbai High offshore field & port approaches"},
    "persian_gulf": {"name": "Persian Gulf / Strait of Hormuz", "bbox": [48.0, 23.5, 58.0, 30.0]},
    "north_sea": {"name": "North Sea (BE / NL / UK sector)", "bbox": [-2.0, 51.0, 6.0, 56.0]},
    "gulf_of_mexico": {"name": "Gulf of Mexico (US sector)", "bbox": [-97.0, 24.0, -84.0, 30.5]},
    "malacca": {"name": "Strait of Malacca / Singapore", "bbox": [98.0, -1.0, 105.0, 6.5]},
    "japan_tokyo_bay": {"name": "Japan · Tokyo Bay approaches", "bbox": [138.5, 33.5, 141.5, 35.8]},
}
MAX_AIS_SPAN_DEG = 20.0  # AISStream accepts large boxes but investigation subscriptions are split for practical volume/limits
MAX_AIS_BOXES = 12


def bbox_of(geometry: dict) -> list:
    return [round(x, 4) for x in shape(geometry).bounds]  # W,S,E,N


def split_bbox_swne(south, west, north, east, span=MAX_AIS_SPAN_DEG) -> list:
    """Split a [S,W,N,E] box into ≤ span° tiles; caps tile count to stay within practical subscription limits."""
    import math
    ny, nx = max(1, math.ceil((north - south) / span)), max(1, math.ceil((east - west) / span))
    if ny * nx > MAX_AIS_BOXES:
        f = math.sqrt(ny * nx / MAX_AIS_BOXES)
        ny, nx = max(1, round(ny / f)), max(1, round(nx / f))
    dy, dx = (north - south) / ny, (east - west) / nx
    return [ais_live.validate_bbox(round(south + j * dy, 4), round(west + i * dx, 4), round(south + (j + 1) * dy, 4), round(west + (i + 1) * dx, 4)) for j in range(ny) for i in range(nx)]


def ais_boxes_for(geometry: dict) -> list:
    w, s, e, n = bbox_of(geometry)
    s, w, n, e = ais_live.expand_bbox(s, w, n, e)
    return split_bbox_swne(s, w, n, e)


async def select(kind: str, actor: str, zone_id: Optional[str] = None, geometry: Optional[dict] = None, preset: Optional[str] = None, name: Optional[str] = None, apply_ais: bool = True) -> dict:
    if kind == "zone":
        z = await db.jurisdictions.find_one({"$or": [{"id": zone_id}, {"code": zone_id}]}, {"_id": 0})
        if not z:
            raise ValueError("zone not found")
        geom, label, prov, ref = z["geometry"], z["name"], z.get("provenance", "REFERENCE"), z["code"]
    elif kind == "preset":
        p = PRESETS.get(preset or "")
        if not p:
            raise ValueError(f"unknown preset; choose one of {list(PRESETS)}")
        geom, label, prov, ref = mapping(box(*p["bbox"])), p["name"], "PRESET", preset
    elif kind == "custom":
        if not geometry:
            raise ValueError("geometry required for a custom AOI")
        validate_polygon(geometry)
        geom, label, prov, ref = geometry, name or "custom maritime AOI", "USER-DEFINED", None
    else:
        raise ValueError("kind must be zone | preset | custom")
    bbox = bbox_of(geom)
    if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 180 * 90:
        raise ValueError("AOI too large — select a region smaller than a hemisphere")
    boxes = ais_boxes_for(geom)
    if kind == "zone":
        zp = zone_public(z)
        juris = {"inside_zone": True, "country": zp.get("country_name") or zp["country"], "country_code": zp["country_code"], "zone": zp["code"], "zone_type": zp["zone_type"], "authority_status": zp["authority_status"], "provenance": zp["provenance"], "note": None}
    else:
        juris = await lookup(geom)
    doc = {"key": "investigation_aoi", "kind": kind, "name": label, "provenance": prov, "ref": ref, "geometry": geom, "bbox": bbox, "ais_bboxes_swne": boxes,
           "jurisdiction": {k: juris.get(k) for k in ("inside_zone", "country", "country_code", "zone", "zone_type", "authority_status", "provenance", "note")},
           "sentinel": {"bbox": bbox, "intersects": geom if kind != "preset" else None, "collection": "sentinel-1-grd"},
           "updated_at": datetime.now(timezone.utc), "updated_by": actor}
    await db.settings.update_one({"key": "investigation_aoi"}, {"$set": doc}, upsert=True)
    if apply_ais:
        doc["ais_coverage"] = await ais_live.set_coverage(boxes, "manual", f"AOI · {label}", actor, ref)
    return doc


async def current() -> Optional[dict]:
    return await db.settings.find_one({"key": "investigation_aoi"}, {"_id": 0})


async def clear(actor: str) -> dict:
    await db.settings.delete_one({"key": "investigation_aoi"})
    await db.settings.delete_one({"key": "ais_coverage"})
    ais_live._reconnect_event.set()
    return {"cleared": True, "ais_coverage": await ais_live.get_coverage()}
