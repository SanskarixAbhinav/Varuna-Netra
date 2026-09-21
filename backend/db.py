import os
import uuid
from typing import Any, Optional
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import pymongo

load_dotenv(Path(__file__).parent / ".env")

client = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]


async def ensure_indexes() -> None:
    await db.ais_positions.create_index([("location", pymongo.GEOSPHERE)])
    await db.ais_positions.create_index([("timestamp", 1)])
    await db.ais_positions.create_index([("mmsi", 1), ("timestamp", 1)])
    await db.ais_positions.create_index([("dedup_hash", 1)], unique=True)
    await db.spill_observations.create_index([("centroid", pymongo.GEOSPHERE)])
    await db.spill_observations.create_index([("acquisition_time", -1)])
    await db.scenes.create_index([("footprint", pymongo.GEOSPHERE)])
    await db.scenes.create_index([("provider", 1), ("provider_scene_id", 1)], unique=True)
    await db.scenes.create_index([("acquisition_time", -1)])
    await db.cases.create_index([("scene_id", 1)])
    await db.dark_vessel_scans.create_index([("case_id", 1), ("created_at", -1)])
    await db.cases.create_index([("created_at", -1)])
    await db.cases.create_index([("attribution_status", 1)])
    await db.correlation_results.create_index([("case_id", 1), ("version", -1)], unique=True)
    await db.reviews.create_index([("case_id", 1), ("created_at", 1)])
    await db.audit_events.create_index([("entity_id", 1), ("created_at", 1)])
    await db.jobs.create_index([("created_at", -1)])
    await db.alerts.create_index([("created_at", -1)])
    await db.watchlist.create_index([("mmsi", 1), ("active", 1)])
    await db.share_links.create_index("token_hash")
    await db.settings.create_index("key", unique=True)
    await db.attachments.create_index([("case_id", 1), ("is_deleted", 1)])
    await db.zone_rules.create_index([("zone_code", 1), ("active", 1)])
    await db.alerts.create_index([("case_id", 1), ("kind", 1), ("rule_id", 1)])
    await db.cases.create_index([("acquisition_time", -1), ("detection_confidence", -1)])
    await db.cases.create_index([("primary_jurisdiction.code", 1), ("acquisition_time", -1)])
    await db.cron_runs.create_index("run_id", unique=True)
    await db.scene_watches.create_index("active")
    await db.detector_feedback.create_index([("case_id", 1), ("created_at", -1)])
    await db.detector_feedback.create_index("detector_version")


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean(doc: Any) -> Any:
    """Strip Mongo _id and make naive datetimes UTC-aware (recursively)."""
    if isinstance(doc, list):
        return [clean(d) for d in doc]
    if isinstance(doc, dict):
        return {k: clean(v) for k, v in doc.items() if k != "_id"}
    if isinstance(doc, datetime):
        return to_utc(doc)
    return doc


async def audit(entity_type: str, entity_id: str, action: str, payload: Optional[dict] = None, actor: str = "system") -> dict:
    ev = {
        "id": str(uuid.uuid4()),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": action,
        "actor": actor,
        "payload": payload or {},
        "created_at": datetime.now(timezone.utc),
    }
    await db.audit_events.insert_one(dict(ev))
    return ev
