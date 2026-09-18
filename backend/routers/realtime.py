from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user, require_role
from db import db, clean
from jobs import enqueue
from livemode import data_mode, provenance, purge_demo_data, seed_india_watches, system_health

router = APIRouter()


@router.get("/system/health")
async def health(user=Depends(get_current_user)):
    return clean(await system_health())


@router.get("/system/data-mode")
async def mode(user=Depends(get_current_user)):
    return clean(await data_mode())


@router.post("/system/purge-demo")
async def purge(user=Depends(require_role("admin"))):
    return {"purged": await purge_demo_data(user["email"]), "data_mode": clean(await data_mode())}


@router.post("/scene-watches/seed-india", status_code=201)
async def seed_india(user=Depends(require_role("supervisor"))):
    return {"added": await seed_india_watches(user["email"]), "total_active": await db.scene_watches.count_documents({"active": True})}


@router.post("/scene-watches/ingest-now", status_code=202)
async def ingest_now(days: int = Query(7, ge=1, le=30), user=Depends(require_role("supervisor"))):
    """Queue a poll of every active watch over the last N days: registers real Sentinel-1 scenes and runs the experimental detector."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    watches = await db.scene_watches.find({"active": True}, {"_id": 0}).to_list(100)
    await db.scene_watches.update_many({"active": True}, {"$set": {"last_polled": since}})
    jobs = [await enqueue("scene_watch_poll", {"watch_id": w["id"]}, actor=user["email"]) for w in watches]
    return {"queued": len(jobs), "job_ids": [j["id"] for j in jobs], "since": since}


@router.get("/cases/{case_id}/provenance")
async def case_provenance(case_id: str, user=Depends(get_current_user)):
    try:
        return clean(await provenance(case_id))
    except ValueError as e:
        raise HTTPException(404, str(e))
