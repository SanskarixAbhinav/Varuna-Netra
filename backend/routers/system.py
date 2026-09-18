from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user, require_role, ROLES
from db import db, clean, audit
from models import REASON_CODES, ATTRIBUTION_STATUSES, SPILL_QUALITY_FLAGS, AIS_QUALITY_FLAGS, CorrelationParams
from correlation import ALGORITHM_VERSION
from services import MOCK_DETECTOR_VERSION
from dashboard import compute_summary, real_alert_filter, REAL_ORIGINS

router = APIRouter()


@router.get("/jobs")
async def list_jobs(status: Optional[str] = None, limit: int = Query(100, le=500), user=Depends(get_current_user)):
    q = {"status": status} if status else {}
    return clean(await db.jobs.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit))


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, user=Depends(get_current_user)):
    job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(404, "job not found")
    return clean(job)


@router.get("/alerts")
async def list_alerts(unacknowledged: bool = False, include_demo: bool = False, limit: int = Query(100, le=500), user=Depends(get_current_user)):
    q = {"acknowledged": False} if unacknowledged else {}
    if not include_demo:
        excluded = [c["id"] for c in await db.cases.find({"origin": {"$nin": REAL_ORIGINS}}, {"_id": 0, "id": 1}).to_list(10000)]
        q = {**real_alert_filter(excluded), **q}
    return clean(await db.alerts.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit))


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: str, user=Depends(require_role("supervisor"))):
    res = await db.alerts.find_one_and_update({"id": alert_id}, {"$set": {"acknowledged": True, "acknowledged_by": user["email"], "acknowledged_at": datetime.now(timezone.utc)}},
                                              projection={"_id": 0}, return_document=True)
    if not res:
        raise HTTPException(404, "alert not found")
    await audit("alert", alert_id, "alert.acknowledged", {"role": user["role"]}, user["email"])
    return clean(res)


@router.get("/audit")
async def list_audit(entity_id: Optional[str] = None, limit: int = Query(200, le=2000), user=Depends(require_role("analyst"))):
    q = {"entity_id": entity_id} if entity_id else {}
    return clean(await db.audit_events.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit))


@router.get("/config/defaults")
async def config_defaults(user=Depends(get_current_user)):
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "mock_detector_version": MOCK_DETECTOR_VERSION,
        "correlation_params": CorrelationParams().model_dump(),
        "attribution_statuses": ATTRIBUTION_STATUSES,
        "spill_quality_flags": SPILL_QUALITY_FLAGS,
        "ais_quality_flags": AIS_QUALITY_FLAGS,
        "reason_codes": REASON_CODES,
        "roles": ROLES,
        "permissions": {"analyst": ["ingest", "correlate", "review", "export"], "supervisor": ["+acknowledge_alerts", "+override_cases"], "admin": ["+manage_users", "+reseed"]},
        "environment_provider": "open-meteo (ERA5 reanalysis / forecast wind 10 m; Copernicus Marine surface current)",
        "drift_model": "surface drift = 3% of wind speed (downwind) + surface current; wind direction is meteorological (FROM), current is oceanographic (TOWARD)",
    }


@router.get("/dashboard/summary")
async def dashboard_summary(user=Depends(get_current_user)):
    """Canonical counters for header + dashboard. Pure DB aggregation; demo/seed/test records excluded (see dashboard.SEMANTICS)."""
    return clean(await compute_summary(db))


@router.get("/stats")
async def stats(user=Depends(get_current_user)):
    """Legacy shape, now derived from the same canonical summary."""
    s = await compute_summary(db)
    return clean({"cases_total": s["cases"]["total"], "by_attribution_status": {k: s["cases"]["by_attribution_status"].get(k, 0) for k in ATTRIBUTION_STATUSES},
                  "pending_review": s["pending"]["total"], "alerts_unacknowledged": s["alerts"]["unread"], **s["observations"],
                  "jobs_running": s["jobs"]["running"], "jobs_failed": s["jobs"]["failed"], "source": "dashboard/summary"})
