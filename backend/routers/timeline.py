import hashlib
import html
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from auth import get_current_user, require_role
from db import db, clean, audit, to_utc
from models import new_id

router = APIRouter()


def _ev(t, kind, title, detail="", actor=None, role=None, meta=None):
    return {"t": to_utc(t) if isinstance(t, datetime) else t, "kind": kind, "title": title, "detail": detail, "actor": actor, "role": role, "meta": meta or {}}


async def build_timeline(case_id: str):
    case = await db.cases.find_one({"id": case_id}, {"_id": 0})
    if not case:
        raise HTTPException(404, "case not found")
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "raw_input": 0})
    scene = await db.scenes.find_one({"id": case.get("scene_id")}, {"_id": 0}) if case.get("scene_id") else None
    results = await db.correlation_results.find({"case_id": case_id}, {"_id": 0, "processing_log": 0}).sort("version", 1).to_list(100)
    reviews = await db.reviews.find({"case_id": case_id}, {"_id": 0}).sort("created_at", 1).to_list(500)
    alerts = await db.alerts.find({"case_id": case_id}, {"_id": 0}).sort("created_at", 1).to_list(100)
    ids = [case_id, spill["id"]] + ([scene["id"]] if scene else [])
    audits = await db.audit_events.find({"entity_id": {"$in": ids}}, {"_id": 0}).sort("created_at", 1).to_list(2000)
    users = {u["email"]: u for u in await db.users.find({}, {"_id": 0, "email": 1, "name": 1, "role": 1}).to_list(500)}
    role_of = lambda a: (users.get(a) or {}).get("role")  # noqa: E731

    ev = []
    if scene:
        ev.append(_ev(scene["acquisition_time"], "scene", f"Satellite pass — {scene['provider']} {scene['provider_scene_id']}", f"{scene.get('sensor_mode') or ''} {scene.get('polarization') or ''} · storage {scene.get('storage_ref') or '—'}".strip()))
    ev.append(_ev(spill["acquisition_time"], "spill", "Spill observed", f"{spill['source']} · confidence {spill['detection_confidence']:.2f} · {spill['estimated_area_km2']} km² · flags: {', '.join(spill.get('quality_flags') or []) or 'none'} · processing {spill['processing_version']}",
                  meta={"centroid": spill["centroid"]["coordinates"]}))
    latest = results[-1] if results else None
    if latest:
        for c in latest["candidates"][:5]:
            cf = c["evidence"]["closest_fix"]
            ev.append(_ev(cf["timestamp"], "ais", f"Closest approach — #{c['rank']} {c.get('vessel_name') or c['mmsi']}", f"MMSI {c['mmsi']} · {c['evidence']['distance_km']} km from slick · {c['evidence']['time_gap_hours']}h before pass · SOG {cf.get('sog_kn') or '—'} kn COG {cf.get('cog_deg') or '—'}° · score {c['score']:.3f} → {c['status']}",
                         meta={"mmsi": c["mmsi"], "rank": c["rank"], "status": c["status"]}))
    for a in audits:
        act = a["action"]
        if act == "case.opened":
            ev.append(_ev(a["created_at"], "case", f"Case {case['case_number']} opened", f"primary jurisdiction {a['payload'].get('primary_jurisdiction') or 'unassigned'}", a["actor"], role_of(a["actor"])))
        elif act == "spill.environment_fetched":
            p = a["payload"]
            ev.append(_ev(a["created_at"], "env", f"Environment fetched ({p.get('source')})", f"wind {p.get('wind')} · current {p.get('current')}" + (f" · errors {p.get('errors')}" if p.get("errors") else ""), a["actor"], role_of(a["actor"])))
        elif act == "case.jurisdiction_resolved":
            ev.append(_ev(a["created_at"], "case", "Jurisdiction resolved", f"primary {a['payload'].get('primary') or 'unassigned'} · zones {', '.join(a['payload'].get('zones') or []) or '—'}", a["actor"], role_of(a["actor"])))
        elif act == "evidence.exported":
            ev.append(_ev(a["created_at"], "export", f"Evidence exported ({a['payload'].get('format')})", f"result v{a['payload'].get('version')}", a["actor"], role_of(a["actor"])))
        elif act == "case.shared":
            ev.append(_ev(a["created_at"], "export", "Timeline share link created", f"expires {a['payload'].get('expires_at')}", a["actor"], role_of(a["actor"])))
        elif act == "attachment.added":
            p = a["payload"]
            ev.append(_ev(a["created_at"], "attachment", f"Evidence file attached — {p.get('filename')}", f"{p.get('kind')} · {p.get('caption') or 'no caption'} · {(p.get('bytes') or 0) / 1024:.0f} KB", a["actor"], role_of(a["actor"]), {"attachment_id": p.get("attachment_id")}))
        elif act == "attachment.removed":
            ev.append(_ev(a["created_at"], "attachment", f"Evidence file removed — {a['payload'].get('filename')}", "", a["actor"], role_of(a["actor"])))
    for r in results:
        ev.append(_ev(r["created_at"], "correlation", f"Correlation run v{r['version']} → {r['overall_status']}", f"{r['algorithm_version']} · {len(r['candidates'])} candidates · corridor {r['params']['corridor_km']} km · window −{r['params']['window_hours_before']}h/+{r['params']['window_hours_after']}h · {'DEGRADED (no drift inputs)' if r['degraded'] else 'drift-corrected'} · hash {r['input_hash'][:12]}",
                     r.get("actor"), role_of(r.get("actor")), {"version": r["version"], "status": r["overall_status"]}))
    for rv in reviews:
        ev.append(_ev(rv["created_at"], "decision", f"Analyst decision — {rv['decision'].replace('_', ' ')}", f"{'vessel MMSI ' + rv['vessel_mmsi'] + ' · ' if rv.get('vessel_mmsi') else ''}{', '.join(rv.get('reason_codes') or []) or 'no reason codes'}{' · ' + rv['notes'] if rv.get('notes') else ''} · on result v{rv.get('result_version')} · {rv.get('previous_attribution_status')} → {rv.get('override_to') or ('analyst_confirmed' if rv['decision'] == 'confirm' else 'insufficient_evidence' if rv['decision'] == 'reject' else 'unchanged')}",
                     rv.get("analyst"), rv.get("analyst_role"), {"decision": rv["decision"], "vessel_mmsi": rv.get("vessel_mmsi")}))
    for al in alerts:
        ev.append(_ev(al["created_at"], "alert", f"Alert raised ({al.get('kind', 'high_confidence')}, {al['severity']})", al["message"] + (f" · acknowledged by {al.get('acknowledged_by')}" if al.get("acknowledged") else " · unacknowledged") + (f" · email {al['notification']['status']} to {len(al['notification'].get('recipients') or [])}" if al.get("notification") else ""), "system", None))
    attachments = await db.attachments.find({"case_id": case_id, "is_deleted": False}, {"_id": 0}).sort("created_at", 1).to_list(200)
    ev.sort(key=lambda e: e["t"])
    return clean({"case": case, "spill_observation": spill, "scene": scene, "events": ev, "attachments": attachments, "generated_at": datetime.now(timezone.utc),
                  "disclaimer": "Decision-support timeline for inter-agency handover. Attribution statuses are analytical, not legal findings of responsibility."})


@router.get("/cases/{case_id}/timeline")
async def case_timeline(case_id: str, user=Depends(get_current_user)):
    return await build_timeline(case_id)


KIND_COLOR = {"scene": "#9D4EDD", "spill": "#FF2A6D", "ais": "#38BDF8", "case": "#00F0FF", "env": "#C77DFF", "correlation": "#00F0FF", "decision": "#10B981", "alert": "#FF6B00", "export": "#94A3B8", "attachment": "#FFB703"}


def render_timeline_html(tl: dict, shared_by: Optional[str] = None, expires_at: Optional[datetime] = None) -> str:
    case, spill = tl["case"], tl["spill_observation"]
    e = html.escape
    pj = case.get("primary_jurisdiction") or {}
    rows = []
    for ev in tl["events"]:
        t = ev["t"].strftime("%Y-%m-%d %H:%MZ") if isinstance(ev["t"], datetime) else str(ev["t"])[:16]
        who = f"<span class=who>{e(ev['actor'])}{' · ' + e(ev['role']) if ev.get('role') else ''}</span>" if ev.get("actor") else ""
        rows.append(f"<li><span class=dot style='background:{KIND_COLOR.get(ev['kind'], '#94A3B8')}'></span><div class=t>{t}</div><div class=body><div class=title><span class=kind>{e(ev['kind'])}</span>{e(ev['title'])} {who}</div><div class=detail>{e(ev['detail'])}</div></div></li>")
    share_note = f"<p class=share>Shared read-only by {e(shared_by)} · link expires {expires_at.strftime('%Y-%m-%d %H:%MZ')}</p>" if shared_by else ""
    atts = tl.get("attachments") or []
    att_html = ("<h3 style='font-size:14px;margin:26px 0 6px'>Attached source imagery &amp; evidence files</h3><ul class=atts>" + "".join(
        f"<li><span class=kind>{e(a['kind'])}</span> {e(a['original_filename'])} — {e(a.get('caption') or 'no caption')} <span class=who>{e(a['uploaded_by'])}</span></li>" for a in atts) + "</ul>") if atts else ""
    return f"""<!doctype html><html><head><meta charset=utf-8><title>{e(case['case_number'])} — case timeline</title>
<style>body{{margin:0;background:#0A0E17;color:#F8FAFC;font-family:'IBM Plex Sans',Segoe UI,Arial,sans-serif}}.wrap{{max-width:960px;margin:0 auto;padding:40px 28px}}
h1{{font-size:30px;margin:0 0 4px;letter-spacing:-.02em}}.mono{{font-family:'JetBrains Mono',Consolas,monospace;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#94A3B8}}
.card{{background:#162032;border:1px solid #1E293B;border-radius:8px;padding:16px 20px;margin:18px 0;display:grid;grid-template-columns:repeat(4,1fr);gap:12px;font-size:12px}}.card b{{display:block;color:#94A3B8;font-weight:normal;font-size:10px;text-transform:uppercase;letter-spacing:.1em;margin-bottom:3px}}
.status{{display:inline-block;padding:2px 10px;border-radius:999px;border:1px solid #FF6B0088;color:#FF6B00;font-size:11px;text-transform:uppercase;letter-spacing:.08em}}
ul{{list-style:none;padding:0;margin:24px 0 0;border-left:1px solid #334155}}li{{position:relative;display:grid;grid-template-columns:150px 1fr;gap:14px;padding:10px 0 10px 22px}}
.dot{{position:absolute;left:-5px;top:16px;width:9px;height:9px;border-radius:50%;box-shadow:0 0 0 3px #0A0E17}}.t{{font-family:Consolas,monospace;font-size:12px;color:#CBD5E1}}
.title{{font-size:14px;font-weight:600}}.kind{{font-family:Consolas,monospace;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:#94A3B8;margin-right:8px}}.who{{font-size:11px;color:#00F0FF;font-weight:normal;margin-left:6px}}
.detail{{font-size:12px;color:#94A3B8;margin-top:3px;line-height:1.5}}.atts{{border:0;margin-top:6px}}.atts li{{display:block;padding:4px 0;font-size:12px}}.disc{{font-size:11px;color:#64748B;border-top:1px solid #1E293B;margin-top:28px;padding-top:12px}}.share{{font-size:11px;color:#FFB703}}
@media print{{body{{background:#fff;color:#111}}.card{{background:#f3f4f6;border-color:#ddd}}.detail,.mono,.card b{{color:#555}}ul{{border-color:#999}}.dot{{box-shadow:0 0 0 3px #fff}}}}</style></head><body><div class=wrap>
<div class=mono>Varuna Netra · case timeline · generated {tl['generated_at'].strftime('%Y-%m-%d %H:%MZ')}</div>
<h1>{e(case['case_number'])}</h1><span class=status>{e(case['attribution_status'].replace('_', ' '))}</span> {share_note}
<div class=card><div><b>Acquired (UTC)</b>{case['acquisition_time'].strftime('%Y-%m-%d %H:%MZ')}</div><div><b>Source</b>{e(spill['source'])}</div><div><b>Detection confidence</b>{spill['detection_confidence']:.2f}</div><div><b>Area</b>{spill['estimated_area_km2']} km²</div>
<div><b>Primary jurisdiction</b>{e(pj.get('code') or 'unassigned')}</div><div><b>Authority</b>{e(pj.get('authority') or '—')}</div><div><b>Review state</b>{e(case['review_state'])}</div><div><b>Confirmed vessel</b>{e(case.get('confirmed_vessel_mmsi') or '—')}</div></div>
<ul>{''.join(rows)}</ul>{att_html}
<p class=disc>{e(tl['disclaimer'])}</p></div></body></html>"""


@router.get("/cases/{case_id}/timeline.html", response_class=HTMLResponse)
async def case_timeline_html(case_id: str, user=Depends(get_current_user)):
    tl = await build_timeline(case_id)
    await audit("case", case_id, "evidence.exported", {"format": "timeline_html"}, user["email"])
    return HTMLResponse(render_timeline_html(tl))


class ShareRequest(BaseModel):
    expires_hours: int = Field(default=168, ge=1, le=720)
    recipient_note: Optional[str] = None


@router.post("/cases/{case_id}/share", status_code=201)
async def create_share(case_id: str, body: ShareRequest, request: Request, user=Depends(require_role("supervisor"))):
    case = await db.cases.find_one({"id": case_id}, {"_id": 0, "case_number": 1})
    if not case:
        raise HTTPException(404, "case not found")
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=body.expires_hours)
    doc = {"id": new_id(), "case_id": case_id, "case_number": case["case_number"], "token_hash": hashlib.sha256(token.encode()).hexdigest(),
           "created_by": user["email"], "recipient_note": body.recipient_note, "expires_at": exp, "revoked": False, "views": 0, "created_at": now}
    await db.share_links.insert_one(dict(doc))
    base = os.environ["FRONTEND_URL"].rstrip("/")
    url = f"{base}/api/share/{token}"
    await audit("case", case_id, "case.shared", {"share_id": doc["id"], "expires_at": exp.isoformat(), "note": body.recipient_note}, user["email"])
    return clean({**{k: v for k, v in doc.items() if k != "token_hash"}, "url": url})


@router.get("/cases/{case_id}/shares")
async def list_shares(case_id: str, user=Depends(get_current_user)):
    return clean(await db.share_links.find({"case_id": case_id}, {"_id": 0, "token_hash": 0}).sort("created_at", -1).to_list(100))


@router.delete("/cases/{case_id}/shares/{share_id}")
async def revoke_share(case_id: str, share_id: str, user=Depends(require_role("supervisor"))):
    res = await db.share_links.update_one({"id": share_id, "case_id": case_id}, {"$set": {"revoked": True, "revoked_by": user["email"], "revoked_at": datetime.now(timezone.utc)}})
    if not res.matched_count:
        raise HTTPException(404, "share link not found")
    await audit("case", case_id, "case.share_revoked", {"share_id": share_id}, user["email"])
    return {"ok": True}


@router.get("/share/{token}", response_class=HTMLResponse)
async def view_share(token: str):
    rec = await db.share_links.find_one({"token_hash": hashlib.sha256(token.encode()).hexdigest()})
    if not rec or rec.get("revoked"):
        return HTMLResponse("<h3 style='font-family:sans-serif'>This share link is invalid or has been revoked.</h3>", status_code=404)
    if to_utc(rec["expires_at"]) < datetime.now(timezone.utc):
        return HTMLResponse("<h3 style='font-family:sans-serif'>This share link has expired.</h3>", status_code=410)
    await db.share_links.update_one({"id": rec["id"]}, {"$inc": {"views": 1}, "$set": {"last_viewed_at": datetime.now(timezone.utc)}})
    tl = await build_timeline(rec["case_id"])
    return HTMLResponse(render_timeline_html(tl, shared_by=rec["created_by"], expires_at=to_utc(rec["expires_at"])))
