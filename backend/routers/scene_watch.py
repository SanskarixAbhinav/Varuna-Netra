import hmac
import os
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import get_current_user, require_role
from db import db, clean, audit
from jobs import enqueue, process
from models import new_id
from scene_watch import collection_ok

router = APIRouter()


class WatchCreate(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    bbox: List[float] = Field(min_length=4, max_length=4)
    collection: str = "sentinel-1-grd"
    auto_detect: bool = True
    note: Optional[str] = None


class WatchUpdate(BaseModel):
    active: Optional[bool] = None
    auto_detect: Optional[bool] = None
    note: Optional[str] = None


@router.get("/scene-watches")
async def list_watches(user=Depends(get_current_user)):
    return clean(await db.scene_watches.find({}, {"_id": 0}).sort("created_at", -1).to_list(100))


@router.post("/scene-watches", status_code=201)
async def create_watch(body: WatchCreate, user=Depends(require_role("supervisor"))):
    w, s, e, n = body.bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90) or (e - w) * (n - s) > 900:
        raise HTTPException(400, "bbox must be [west, south, east, north] and under ~30°×30°")
    if not collection_ok(body.collection):
        raise HTTPException(400, "unknown collection")
    doc = {**body.model_dump(), "id": new_id(), "active": True, "last_polled": None, "scenes_registered": 0, "polls": 0, "created_by": user["email"], "created_at": datetime.now(timezone.utc)}
    await db.scene_watches.insert_one(dict(doc))
    await audit("scene_watch", doc["id"], "scene_watch.created", {"name": body.name, "bbox": body.bbox, "collection": body.collection, "auto_detect": body.auto_detect}, user["email"])
    return clean(doc)


@router.patch("/scene-watches/{watch_id}")
async def update_watch(watch_id: str, body: WatchUpdate, user=Depends(require_role("supervisor"))):
    update = body.model_dump(exclude_none=True)
    res = await db.scene_watches.find_one_and_update({"id": watch_id}, {"$set": {**update, "updated_at": datetime.now(timezone.utc)}}, projection={"_id": 0}, return_document=True)
    if not res:
        raise HTTPException(404, "watch not found")
    await audit("scene_watch", watch_id, "scene_watch.updated", update, user["email"])
    return clean(res)


@router.delete("/scene-watches/{watch_id}")
async def delete_watch(watch_id: str, user=Depends(require_role("supervisor"))):
    if not (await db.scene_watches.delete_one({"id": watch_id})).deleted_count:
        raise HTTPException(404, "watch not found")
    await audit("scene_watch", watch_id, "scene_watch.deleted", {}, user["email"])
    return {"ok": True}


@router.post("/scene-watches/{watch_id}/run", status_code=202)
async def run_watch(watch_id: str, sync: bool = False, user=Depends(require_role("supervisor"))):
    if not await db.scene_watches.find_one({"id": watch_id}):
        raise HTTPException(404, "watch not found")
    job = await enqueue("scene_watch_poll", {"watch_id": watch_id}, user["email"], inline=sync)
    if sync:
        job = await process(job["id"])
    return clean(job)


@router.post("/cron/scene-watch", status_code=202)
async def cron_scene_watch(request: Request):
    # Cron endpoints must ack 2xx immediately; enqueue/background the actual work.
    auth = request.headers.get("Authorization", "")
    secret = os.environ.get("WEBHOOK_CRON_SECRET", "")
    if not auth.startswith("Bearer ") or not secret or not hmac.compare_digest(auth[7:], secret):
        raise HTTPException(401, "unauthorized")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    run_id = request.headers.get("X-Webhook-Id") or (body or {}).get("run_id") or new_id()
    if await db.cron_runs.find_one({"run_id": run_id}):
        return {"ok": True, "duplicate": True, "run_id": run_id}
    await db.cron_runs.insert_one({"run_id": run_id, "schedule": (body or {}).get("schedule_id", "scene-watch"), "received_at": datetime.now(timezone.utc)})
    job = await enqueue("scene_watch_poll", {"run_id": run_id}, "cron")
    return {"ok": True, "run_id": run_id, "job_id": job["id"]}
