import logging
from datetime import datetime, timedelta, timezone

from db import db, audit, to_utc
from jobs import handler, job_log
from models import new_id, SceneCreate
from notifications import notify_alert
from satellite import search_scenes, COLLECTIONS

logger = logging.getLogger("scene_watch")


async def poll_watch(w: dict, job_id: str | None = None) -> dict:
    from services import create_scene
    from detector import detect_scene
    now = datetime.now(timezone.utc)
    since = to_utc(w.get("last_polled")) or (now - timedelta(days=3))
    res = await search_scenes(w["bbox"], since.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ"), w.get("collection", "sentinel-1-grd"), 50)
    registered, detections = [], []
    for it in res["scenes"]:
        if await db.scenes.find_one({"provider_scene_id": it["stac_id"]}):
            continue
        payload = SceneCreate(provider=it["provider"], provider_scene_id=it["stac_id"], sensor_mode=it.get("instrument_mode") or it.get("product_type"),
                              polarization="+".join(it["polarizations"]) if it.get("polarizations") else None, acquisition_time=it["datetime"], footprint=it["footprint"], storage_ref=it["stac_href"],
                              metadata={"stac_collection": it["collection"], "platform": it.get("platform"), "orbit_state": it.get("orbit_state"), "bbox": it.get("bbox"), "cloud_cover": it.get("cloud_cover"),
                                        "preview_href": it.get("preview_href"), "thumbnail_href": it.get("thumbnail_href"), "source": "Microsoft Planetary Computer STAC", "scene_watch_id": w["id"]})
        scene = await create_scene(payload, f"scene-watch:{w['name']}")
        registered.append(scene["provider_scene_id"])
        det = None
        if w.get("auto_detect"):
            try:
                det = await detect_scene(scene, f"scene-watch:{w['name']}")
                detections.append({"scene": scene["provider_scene_id"], **{k: det[k] for k in ("detector", "spots", "cases")}})
            except Exception as e:  # noqa: BLE001
                logger.error("auto detect failed for %s: %s", scene["provider_scene_id"], e)
        alert = {"id": new_id(), "case_id": (det or {}).get("cases", [{}])[0].get("case_id") if det and det.get("cases") else None, "case_number": (det or {}).get("cases", [{}])[0].get("case_number") if det and det.get("cases") else None,
                 "severity": "medium" if det and det.get("spots") else "low", "kind": "new_scene", "acknowledged": False, "scene_id": scene["id"], "watch_id": w["id"], "created_at": now,
                 "message": f"SCENE WATCH '{w['name']}': new {it['platform'] or it['provider']} pass {it['stac_id']} acquired {it['datetime'][:16]}Z" + (f" — experimental detector found {det['spots']} dark spot(s): {', '.join(c['case_number'] for c in det['cases'])}" if det and det.get("spots") else (" — no dark spots detected" if det else ""))}
        await db.alerts.insert_one(dict(alert))
        await audit("alert", alert["id"], "alert.new_scene", {"scene_id": scene["id"], "watch_id": w["id"]}, "system")
        case = await db.cases.find_one({"id": alert["case_id"]}, {"_id": 0}) if alert["case_id"] else {"id": None, "case_number": it["stac_id"], "acquisition_time": to_utc(scene["acquisition_time"]), "attribution_status": "n/a"}
        await notify_alert(alert, case)
        if job_id:
            await job_log(job_id, f"[{w['name']}] registered {it['stac_id']}" + (f" · {det['spots']} spots" if det else ""))
    await db.scene_watches.update_one({"id": w["id"]}, {"$set": {"last_polled": now, "last_result": {"searched": res["count"], "registered": len(registered), "at": now}}, "$inc": {"scenes_registered": len(registered), "polls": 1}})
    return {"watch": w["name"], "searched": res["count"], "registered": registered, "detections": detections}


@handler("scene_watch_poll")
async def handle_scene_watch_poll(job):
    q = {"active": True}
    if job["payload"].get("watch_id"):
        q = {"id": job["payload"]["watch_id"]}
    out = []
    for w in await db.scene_watches.find(q, {"_id": 0}).to_list(100):
        try:
            out.append(await poll_watch(w, job["id"]))
        except Exception as e:  # noqa: BLE001
            await job_log(job["id"], f"[{w['name']}] poll failed: {str(e)[:200]}", "error")
            out.append({"watch": w["name"], "error": str(e)[:200]})
    return {"watches": out}


def collection_ok(c: str) -> bool:
    return c in COLLECTIONS
