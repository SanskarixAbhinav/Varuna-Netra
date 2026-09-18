import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import aoi
import ais_live
from aoi import bbox_of
from auth import get_current_user, require_role
from db import db, clean, audit
from geo import validate_polygon
from jurisdiction import ZONE_TYPES, apply_to_case, lookup, provenance_of, zone_public
from models import GeoJSONGeometry, new_id

router = APIRouter()


class ZoneCreate(BaseModel):
    code: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=2)
    authority: str = Field(min_length=2)
    country: Optional[str] = None
    zone_type: str = "eez"
    geometry: GeoJSONGeometry
    contact: Optional[str] = None
    active: bool = True


class ZoneUpdate(BaseModel):
    name: Optional[str] = None
    authority: Optional[str] = None
    country: Optional[str] = None
    zone_type: Optional[str] = None
    geometry: Optional[GeoJSONGeometry] = None
    contact: Optional[str] = None
    active: Optional[bool] = None


@router.get("/jurisdictions")
async def list_zones(q: Optional[str] = None, zone_type: Optional[str] = None, country: Optional[str] = None, active: Optional[bool] = None, include_geometry: bool = False,
                     limit: int = Query(100, le=500), offset: int = Query(0, ge=0), user=Depends(get_current_user)):
    """Paginated zone catalogue WITHOUT geometry by default (worldwide dataset is too large for the browser); use /jurisdictions/geojson for map features."""
    flt = {}
    if q:
        rx = {"$regex": re.escape(q.strip()[:80]), "$options": "i"}
        flt["$or"] = [{"code": rx}, {"name": rx}, {"country": rx}, {"country_name": rx}, {"territory": rx}, {"authority": rx}]
    if zone_type:
        flt["zone_type"] = zone_type
    if country:
        flt["country"] = country.upper()
    if active is not None:
        flt["active"] = active
    proj = {"_id": 0, "geometry_low": 0} if include_geometry else {"_id": 0, "geometry": 0, "geometry_low": 0}
    total = await db.jurisdictions.count_documents(flt)
    rows = await db.jurisdictions.find(flt, proj).sort([("country", 1), ("zone_type", 1), ("code", 1)]).skip(offset).to_list(limit)
    for z in rows:
        z["provenance"] = provenance_of(z)
    ds = await db.settings.find_one({"key": "jurisdiction_dataset"}, {"_id": 0})
    return clean({"total": total, "offset": offset, "limit": limit, "zones": rows, "dataset": ds,
                  "by_type": {r["_id"]: r["n"] for r in await db.jurisdictions.aggregate([{"$match": {"active": True}}, {"$group": {"_id": "$zone_type", "n": {"$sum": 1}}}]).to_list(10)},
                  "countries": await db.jurisdictions.distinct("country", {"active": True})})


def _bbox_poly(bbox: str) -> dict:
    try:
        w, s, e, n = [float(x) for x in bbox.split(",")]
    except ValueError:
        raise HTTPException(400, "bbox must be west,south,east,north")
    w, e = max(-180.0, w), min(180.0, e)
    s, n = max(-89.9, s), min(89.9, n)
    if w >= e or s >= n:
        raise HTTPException(400, "bbox must be west,south,east,north with west<east and south<north")
    if e - w >= 359:  # whole-world view: 2dsphere cannot index a full-sphere polygon; use two hemispheres
        return {"type": "MultiPolygon", "coordinates": [[[[-179.99, s], [0, s], [0, n], [-179.99, n], [-179.99, s]]], [[[0, s], [179.99, s], [179.99, n], [0, n], [0, s]]]]}
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


@router.get("/jurisdictions/geojson")
async def zones_geojson(bbox: Optional[str] = None, zone_type: Optional[str] = None, country: Optional[str] = None, detail: str = "low", ids: Optional[str] = None,
                        limit: int = Query(150, le=400), user=Depends(get_current_user)):
    """Viewport-driven features: only zones intersecting the bbox, simplified geometry unless detail=high."""
    flt = {"active": True}
    if bbox:
        flt["geometry"] = {"$geoIntersects": {"$geometry": _bbox_poly(bbox)}}
    if zone_type:
        flt["zone_type"] = {"$in": zone_type.split(",")}
    if country:
        flt["country"] = country.upper()
    if ids:
        flt["$or"] = [{"id": {"$in": ids.split(",")}}, {"code": {"$in": ids.split(",")}}]
    if not bbox and not ids and not country:
        raise HTTPException(400, "provide bbox (viewport), ids or country — the worldwide dataset is never sent whole")
    zones = await db.jurisdictions.find(flt, {"_id": 0}).to_list(limit)
    feats = []
    for z in zones:
        g = z.get("geometry_low") if detail == "low" and z.get("geometry_low") else z["geometry"]
        feats.append({"type": "Feature", "geometry": g, "properties": {**zone_public(z), "detail": "low" if g is z.get("geometry_low") else "high", "bbox": z.get("bbox")}})
    return {"type": "FeatureCollection", "features": feats, "count": len(feats), "truncated": len(zones) >= limit, "detail": detail}


@router.get("/jurisdictions/{zone_id}/geometry")
async def zone_geometry(zone_id: str, detail: str = "high", user=Depends(get_current_user)):
    z = await db.jurisdictions.find_one({"$or": [{"id": zone_id}, {"code": zone_id}]}, {"_id": 0})
    if not z:
        raise HTTPException(404, "zone not found")
    g = z.get("geometry_low") if detail == "low" and z.get("geometry_low") else z["geometry"]
    return clean({"type": "Feature", "geometry": g, "properties": {**zone_public(z), "bbox": z.get("bbox") or bbox_of(z["geometry"]), "dataset": z.get("dataset"), "area_km2": z.get("area_km2"), "vertices": z.get("vertices")}})


class LookupBody(BaseModel):
    geometry: Optional[GeoJSONGeometry] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


@router.post("/jurisdictions/lookup")
async def jurisdiction_lookup(body: LookupBody, user=Depends(get_current_user)):
    """Geospatial intersection → country / zone / provenance. inside_zone=false for high seas; never infers legal responsibility."""
    if body.geometry:
        geom = body.geometry.model_dump()
    elif body.lat is not None and body.lon is not None:
        geom = {"type": "Point", "coordinates": [body.lon, body.lat]}
    else:
        raise HTTPException(400, "provide geometry or lat/lon")
    try:
        return clean(await lookup(geom))
    except ValueError as e:
        raise HTTPException(400, str(e))


class AoiSelect(BaseModel):
    kind: str
    zone_id: Optional[str] = None
    preset: Optional[str] = None
    geometry: Optional[GeoJSONGeometry] = None
    name: Optional[str] = None
    apply_ais: bool = True


@router.get("/aoi")
async def get_aoi(user=Depends(get_current_user)):
    cur = await aoi.current()
    return clean({"aoi": cur, "presets": {k: {**v} for k, v in aoi.PRESETS.items()}, "ais_coverage": await ais_live.get_coverage()})


@router.post("/aoi/select")
async def select_aoi(body: AoiSelect, user=Depends(require_role("analyst"))):
    try:
        doc = await aoi.select(body.kind, user["email"], body.zone_id, body.geometry.model_dump() if body.geometry else None, body.preset, body.name, body.apply_ais)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await audit("settings", "investigation_aoi", "aoi.selected", {"kind": body.kind, "ref": doc.get("ref"), "bbox": doc["bbox"], "ais_boxes": len(doc["ais_bboxes_swne"])}, user["email"])
    return clean(doc)


@router.delete("/aoi")
async def clear_aoi(user=Depends(require_role("analyst"))):
    await audit("settings", "investigation_aoi", "aoi.cleared", {}, user["email"])
    return clean(await aoi.clear(user["email"]))


@router.post("/jurisdictions", status_code=201)
async def create_zone(body: ZoneCreate, user=Depends(require_role("admin"))):
    if body.zone_type not in ZONE_TYPES:
        raise HTTPException(400, f"zone_type must be one of {ZONE_TYPES}")
    try:
        validate_polygon(body.geometry.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    if await db.jurisdictions.find_one({"code": body.code}):
        raise HTTPException(400, "zone code already exists")
    now = datetime.now(timezone.utc)
    doc = {**body.model_dump(), "id": new_id(), "source": f"uploaded by {user['email']}", "provenance": "USER-DEFINED", "authority_status": "USER-DEFINED", "official": False, "bbox": bbox_of(body.geometry.model_dump()), "created_at": now, "updated_at": now}
    await db.jurisdictions.insert_one(dict(doc))
    await audit("jurisdiction", doc["id"], "jurisdiction.created", {"code": body.code, "zone_type": body.zone_type}, user["email"])
    return clean(doc)


@router.put("/jurisdictions/{zone_id}")
async def update_zone(zone_id: str, body: ZoneUpdate, user=Depends(require_role("admin"))):
    update = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if "zone_type" in update and update["zone_type"] not in ZONE_TYPES:
        raise HTTPException(400, f"zone_type must be one of {ZONE_TYPES}")
    if "geometry" in update:
        try:
            validate_polygon(update["geometry"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    update["updated_at"] = datetime.now(timezone.utc)
    res = await db.jurisdictions.find_one_and_update({"id": zone_id}, {"$set": update}, projection={"_id": 0}, return_document=True)
    if not res:
        raise HTTPException(404, "zone not found")
    await audit("jurisdiction", zone_id, "jurisdiction.updated", {k: (v if k != "geometry" else "geometry") for k, v in update.items() if k != "updated_at"}, user["email"])
    return clean(res)


@router.delete("/jurisdictions/{zone_id}")
async def delete_zone(zone_id: str, user=Depends(require_role("admin"))):
    res = await db.jurisdictions.delete_one({"id": zone_id})
    if not res.deleted_count:
        raise HTTPException(404, "zone not found")
    await audit("jurisdiction", zone_id, "jurisdiction.deleted", {}, user["email"])
    return {"ok": True}


@router.post("/jurisdictions/resolve-all")
async def resolve_all(user=Depends(require_role("admin"))):
    ids = [c["id"] async for c in db.cases.find({}, {"id": 1})]
    summary = {}
    for cid in ids:
        _, primary = await apply_to_case(cid, user["email"])
        summary[primary["code"] if primary else "unassigned"] = summary.get(primary["code"] if primary else "unassigned", 0) + 1
    return {"cases": len(ids), "by_primary": summary}


@router.post("/cases/{case_id}/jurisdiction/resolve")
async def resolve_case(case_id: str, user=Depends(require_role("analyst"))):
    if not await db.cases.find_one({"id": case_id}):
        raise HTTPException(404, "case not found")
    zones, primary = await apply_to_case(case_id, user["email"])
    return clean({"jurisdictions": zones, "primary_jurisdiction": primary})
