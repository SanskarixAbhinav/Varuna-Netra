from datetime import datetime, timezone

from db import db, audit
from models import new_id
from notifications import notify_alert


async def evaluate_zone_rules(case_id: str, trigger: str, actor="system") -> list:
    """Raise a zone_rule alert (once per case+rule) for every active rule whose zone intersects the case and whose thresholds pass."""
    case = await db.cases.find_one({"id": case_id}, {"_id": 0})
    if not case:
        return []
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "estimated_area_km2": 1, "detection_confidence": 1})
    codes = {z["code"] for z in case.get("jurisdictions") or []}
    if not codes:
        return []
    rules = await db.zone_rules.find({"active": True, "zone_code": {"$in": sorted(codes)}}, {"_id": 0}).to_list(200)
    raised = []
    for r in rules:
        if r.get("min_area_km2") is not None and (spill.get("estimated_area_km2") or 0) < r["min_area_km2"]:
            continue
        if r.get("min_confidence") is not None and (spill.get("detection_confidence") or 0) < r["min_confidence"]:
            continue
        if r.get("primary_only") and (case.get("primary_jurisdiction") or {}).get("code") != r["zone_code"]:
            continue
        if await db.alerts.find_one({"case_id": case_id, "kind": "zone_rule", "rule_id": r["id"]}):
            continue
        now = datetime.now(timezone.utc)
        alert = {"id": new_id(), "case_id": case_id, "case_number": case["case_number"], "severity": r["severity"], "kind": "zone_rule", "acknowledged": False, "icg": case.get("icg"),
                 "rule_id": r["id"], "rule_name": r["name"], "zone_code": r["zone_code"], "trigger": trigger, "created_at": now,
                 "message": f"ZONE RULE '{r['name']}': spill {case['case_number']} inside {r['zone_code']} ({spill.get('estimated_area_km2')} km², detection confidence {spill.get('detection_confidence'):.2f}) — {r.get('note') or 'requires attention'}"}
        await db.alerts.insert_one(dict(alert))
        await db.zone_rules.update_one({"id": r["id"]}, {"$inc": {"hits": 1}, "$set": {"last_hit_case": case["case_number"], "last_hit_at": now}})
        await audit("alert", alert["id"], "alert.zone_rule", {"case_id": case_id, "rule_id": r["id"], "zone_code": r["zone_code"], "trigger": trigger}, actor)
        await notify_alert(alert, case)
        alert.pop("_id", None)
        raised.append(alert)
    return raised
