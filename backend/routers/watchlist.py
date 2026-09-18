from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user, require_role
from db import db, clean, audit
from jobs import enqueue
from marine_regions import DEFAULT_ISO3
from models import new_id

router = APIRouter()


class ImportRequest(BaseModel):
    iso3: List[str] = DEFAULT_ISO3
    replace_demo: bool = True
    layers: List[str] = ["eez"]


@router.post("/jurisdictions/import/marine-regions", status_code=202)
async def import_marine_regions(body: ImportRequest, user=Depends(require_role("admin"))):
    bad = [i for i in body.iso3 if len(i.strip()) != 3 and i.strip().upper() not in ("ALL", "GLOBAL")]
    if bad or not body.iso3:
        raise HTTPException(400, "iso3 must be a non-empty list of 3-letter ISO codes")
    if any(l not in ("eez", "eez_24nm", "eez_12nm") for l in body.layers) or not body.layers:
        raise HTTPException(400, "layers must be a non-empty subset of eez, eez_24nm, eez_12nm")
    job = await enqueue("import_eez", {"iso3": body.iso3, "replace_demo": body.replace_demo, "layers": body.layers}, user["email"])
    await audit("jurisdiction", "import", "jurisdiction.import_requested", {"iso3": body.iso3, "job_id": job["id"]}, user["email"])
    return clean(job)


class WatchlistCreate(BaseModel):
    mmsi: str = Field(min_length=3, max_length=20)
    vessel_name: Optional[str] = None
    reason: str = Field(min_length=3)
    severity: str = "high"
    expires_at: Optional[datetime] = None


@router.get("/watchlist")
async def list_watchlist(user=Depends(get_current_user)):
    return clean(await db.watchlist.find({}, {"_id": 0}).sort("created_at", -1).to_list(500))


@router.post("/watchlist", status_code=201)
async def add_watchlist(body: WatchlistCreate, user=Depends(require_role("supervisor"))):
    if body.severity not in ("high", "medium", "low"):
        raise HTTPException(400, "severity must be high|medium|low")
    if await db.watchlist.find_one({"mmsi": body.mmsi, "active": True}):
        raise HTTPException(400, "vessel already on the active watchlist")
    if not body.vessel_name:
        last = await db.ais_positions.find_one({"mmsi": body.mmsi}, {"vessel_name": 1}, sort=[("timestamp", -1)])
        body.vessel_name = last.get("vessel_name") if last else None
    now = datetime.now(timezone.utc)
    doc = {**body.model_dump(), "id": new_id(), "active": True, "added_by": user["email"], "added_by_role": user["role"], "created_at": now, "hits": 0}
    await db.watchlist.insert_one(dict(doc))
    await audit("watchlist", doc["id"], "watchlist.added", {"mmsi": body.mmsi, "reason": body.reason, "severity": body.severity}, user["email"])
    return clean(doc)


@router.delete("/watchlist/{entry_id}")
async def remove_watchlist(entry_id: str, user=Depends(require_role("supervisor"))):
    res = await db.watchlist.find_one_and_update({"id": entry_id, "active": True}, {"$set": {"active": False, "removed_by": user["email"], "removed_at": datetime.now(timezone.utc)}})
    if not res:
        raise HTTPException(404, "watchlist entry not found")
    await audit("watchlist", entry_id, "watchlist.removed", {"mmsi": res["mmsi"]}, user["email"])
    return {"ok": True}


async def active_watchlist_map():
    return {w["mmsi"]: w for w in await db.watchlist.find({"active": True}, {"_id": 0}).to_list(1000)}
