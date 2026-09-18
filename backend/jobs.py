import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from db import db
from events import publish
from models import new_id

logger = logging.getLogger("jobs")
Handler = Callable[[dict], Awaitable[Any]]
HANDLERS: Dict[str, Handler] = {}
MAX_ATTEMPTS = 3
_queue: Optional[asyncio.Queue] = None


def handler(job_type: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn
    return deco


async def job_log(job_id: str, msg: str, level: str = "info") -> None:
    await db.jobs.update_one({"id": job_id}, {"$push": {"logs": {"t": datetime.now(timezone.utc).isoformat(), "level": level, "msg": msg}},
                                              "$set": {"updated_at": datetime.now(timezone.utc)}})


async def enqueue(job_type: str, payload: dict, actor: str = "system", inline: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    job = {"id": new_id(), "type": job_type, "status": "queued", "payload": payload, "result": None, "error": None,
           "attempts": 0, "logs": [{"t": now.isoformat(), "level": "info", "msg": f"queued {job_type}"}],
           "actor": actor, "created_at": now, "updated_at": now}
    await db.jobs.insert_one(dict(job))
    if not inline:
        await _queue.put(job["id"])
    job.pop("_id", None)
    return job


async def process(job_id: str) -> Optional[dict]:
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job or job["status"] in ("succeeded",):
        return job
    fn = HANDLERS.get(job["type"])
    attempts = job["attempts"] + 1
    await db.jobs.update_one({"id": job_id}, {"$set": {"status": "running", "attempts": attempts, "started_at": datetime.now(timezone.utc)}})
    await job_log(job_id, f"attempt {attempts} started")
    try:
        result = await fn(job)
        await db.jobs.update_one({"id": job_id}, {"$set": {"status": "succeeded", "result": result, "finished_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}})
        await job_log(job_id, "completed")
        publish("job", {"job_id": job_id, "type": job["type"], "status": "succeeded", "case_id": (job.get("payload") or {}).get("case_id"), "result": result})
    except Exception as e:
        logger.error("job %s failed: %s\n%s", job_id, e, traceback.format_exc())
        if attempts < MAX_ATTEMPTS:
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "queued", "error": str(e)}})
            await job_log(job_id, f"failed: {e} — retrying", "error")
            await _queue.put(job_id)
        else:
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "failed", "error": str(e), "finished_at": datetime.now(timezone.utc)}})
            await job_log(job_id, f"failed permanently: {e}", "error")
            publish("job", {"job_id": job_id, "type": job["type"], "status": "failed", "error": str(e)[:200], "case_id": (job.get("payload") or {}).get("case_id")})
    return await db.jobs.find_one({"id": job_id}, {"_id": 0})


async def worker() -> None:
    while True:
        job_id = await _queue.get()
        try:
            await process(job_id)
        except Exception:
            logger.exception("worker error")
        finally:
            _queue.task_done()


def start() -> None:
    global _queue
    _queue = asyncio.Queue()
    asyncio.create_task(worker())
    asyncio.create_task(_recover_orphans())


async def _recover_orphans() -> None:
    """Queue lives in memory: jobs left queued/running by a previous process would otherwise stay stuck forever."""
    orphans = await db.jobs.find({"status": {"$in": ["queued", "running"]}}, {"_id": 0, "id": 1, "attempts": 1}).sort("created_at", 1).to_list(2000)
    for j in orphans:
        if j.get("attempts", 0) >= MAX_ATTEMPTS:
            await db.jobs.update_one({"id": j["id"]}, {"$set": {"status": "failed", "error": "abandoned: max attempts reached before process restart", "finished_at": datetime.now(timezone.utc)}})
            continue
        await db.jobs.update_one({"id": j["id"]}, {"$set": {"status": "queued"}})
        await job_log(j["id"], "re-queued after backend restart (in-memory queue lost)")
        await _queue.put(j["id"])
    if orphans:
        logger.info("jobs: recovered %d orphaned jobs", len(orphans))
