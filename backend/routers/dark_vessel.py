from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user, require_role
from db import db, clean
from dark_vessel import scan_case

router = APIRouter()


@router.post("/cases/{case_id}/dark-vessels/scan", status_code=201)
async def scan(case_id: str, radius_km: float = Query(40.0, ge=5, le=150), user=Depends(require_role("analyst"))):
    try:
        return clean(await scan_case(case_id, user["email"], radius_km))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/cases/{case_id}/dark-vessels")
async def latest(case_id: str, user=Depends(get_current_user)):
    scan = await db.dark_vessel_scans.find_one({"case_id": case_id}, {"_id": 0}, sort=[("created_at", -1)])
    return clean(scan) if scan else {"case_id": case_id, "targets": [], "dark_count": 0, "status": "not_scanned"}
