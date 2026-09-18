from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import get_current_user, require_role
from db import db, clean, audit
from geo import validate_polygon
from icg import apply_icg_to_case, resolve_icg, REGIONS, APPROX_NOTE

router = APIRouter()


class DistrictUpdate(BaseModel):
    name: Optional[str] = None
    district_hq: Optional[str] = None
    geometry: Optional[dict] = None
    active: Optional[bool] = None
    approximate: Optional[bool] = None
    note: Optional[str] = None
    source: Optional[str] = None
    recipients: Optional[List[str]] = None


@router.get("/icg/districts")
async def list_districts(user=Depends(get_current_user)):
    rows = await db.icg_districts.find({}, {"_id": 0, "geometry": 0}).sort("code", 1).to_list(100)
    return clean({"regions": REGIONS, "districts": rows, "disclaimer": APPROX_NOTE})


@router.get("/icg/districts/geojson")
async def districts_geojson(user=Depends(get_current_user)):
    rows = await db.icg_districts.find({"active": True}, {"_id": 0}).to_list(100)
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": r["geometry"], "properties": {k: v for k, v in r.items() if k not in ("geometry", "created_at", "updated_at")}} for r in rows]}


@router.get("/icg/resolve")
async def resolve(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180), user=Depends(get_current_user)):
    return {"lat": lat, "lon": lon, "icg": await resolve_icg(lat, lon)}


@router.put("/icg/districts/{code}")
async def update_district(code: str, body: DistrictUpdate, user=Depends(require_role("admin"))):
    d = await db.icg_districts.find_one({"code": code})
    if not d:
        raise HTTPException(404, "district not found")
    update = body.model_dump(exclude_none=True)
    if "recipients" in update:
        update["recipients"] = sorted({e.lower().strip() for e in update["recipients"] if "@" in e})
    if "geometry" in update:
        update["geometry"] = validate_polygon(update["geometry"]).__geo_interface__
        update.setdefault("approximate", False)
        update.setdefault("source", f"uploaded by {user['email']}")
    update.update({"updated_at": datetime.now(timezone.utc), "updated_by": user["email"]})
    await db.icg_districts.update_one({"code": code}, {"$set": update})
    await audit("icg_district", d["id"], "icg.district_updated", {k: (v if k != "geometry" else "geometry replaced") for k, v in update.items()}, user["email"])
    return clean(await db.icg_districts.find_one({"code": code}, {"_id": 0, "geometry": 0}))


@router.post("/icg/resolve-all")
async def resolve_all(user=Depends(require_role("admin"))):
    ids = [c["id"] async for c in db.cases.find({}, {"id": 1})]
    routed = 0
    for cid in ids:
        if await apply_icg_to_case(cid, user["email"]):
            routed += 1
    return {"cases": len(ids), "routed_to_icg": routed}


@router.post("/cases/{case_id}/icg/resolve")
async def resolve_case(case_id: str, user=Depends(require_role("analyst"))):
    if not await db.cases.find_one({"id": case_id}, {"id": 1}):
        raise HTTPException(404, "case not found")
    return {"case_id": case_id, "icg": await apply_icg_to_case(case_id, user["email"])}
