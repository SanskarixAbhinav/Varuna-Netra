from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user, require_role
from db import db, clean, audit
from models import new_id
from rules import evaluate_zone_rules

router = APIRouter()


class RuleCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    zone_code: str = Field(min_length=2)
    min_area_km2: Optional[float] = Field(default=None, ge=0)
    min_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    severity: str = "high"
    primary_only: bool = False
    note: Optional[str] = None


class RuleUpdate(BaseModel):
    active: Optional[bool] = None
    severity: Optional[str] = None
    min_area_km2: Optional[float] = Field(default=None, ge=0)
    min_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    note: Optional[str] = None


@router.get("/zone-rules")
async def list_rules(user=Depends(get_current_user)):
    return clean(await db.zone_rules.find({}, {"_id": 0}).sort("created_at", -1).to_list(200))


@router.post("/zone-rules", status_code=201)
async def create_rule(body: RuleCreate, user=Depends(require_role("supervisor"))):
    if body.severity not in ("high", "medium", "low"):
        raise HTTPException(400, "severity must be high|medium|low")
    zone = await db.jurisdictions.find_one({"code": body.zone_code}, {"_id": 0, "name": 1, "authority": 1})
    if not zone:
        raise HTTPException(400, "unknown zone_code")
    doc = {**body.model_dump(), "id": new_id(), "zone_name": zone["name"], "active": True, "hits": 0, "created_by": user["email"], "created_at": datetime.now(timezone.utc)}
    await db.zone_rules.insert_one(dict(doc))
    await audit("zone_rule", doc["id"], "zone_rule.created", {k: v for k, v in body.model_dump().items()}, user["email"])
    return clean(doc)


@router.patch("/zone-rules/{rule_id}")
async def update_rule(rule_id: str, body: RuleUpdate, user=Depends(require_role("supervisor"))):
    update = body.model_dump(exclude_none=True)
    if "severity" in update and update["severity"] not in ("high", "medium", "low"):
        raise HTTPException(400, "severity must be high|medium|low")
    update["updated_at"] = datetime.now(timezone.utc)
    res = await db.zone_rules.find_one_and_update({"id": rule_id}, {"$set": update}, projection={"_id": 0}, return_document=True)
    if not res:
        raise HTTPException(404, "rule not found")
    await audit("zone_rule", rule_id, "zone_rule.updated", {k: v for k, v in update.items() if k != "updated_at"}, user["email"])
    return clean(res)


@router.delete("/zone-rules/{rule_id}")
async def delete_rule(rule_id: str, user=Depends(require_role("supervisor"))):
    res = await db.zone_rules.delete_one({"id": rule_id})
    if not res.deleted_count:
        raise HTTPException(404, "rule not found")
    await audit("zone_rule", rule_id, "zone_rule.deleted", {}, user["email"])
    return {"ok": True}


@router.post("/zone-rules/evaluate")
async def evaluate_all(user=Depends(require_role("supervisor"))):
    ids = [c["id"] async for c in db.cases.find({}, {"id": 1})]
    raised = []
    for cid in ids:
        raised += await evaluate_zone_rules(cid, "manual_evaluation", user["email"])
    return clean({"cases": len(ids), "alerts_raised": len(raised), "alerts": raised})
