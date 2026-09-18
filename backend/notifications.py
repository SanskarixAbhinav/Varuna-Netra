import html
import logging
import os
from datetime import datetime, timezone

from db import db, audit
from emailer import get_config, send_email
from events import publish

logger = logging.getLogger("notifications")
SEV_COLOR = {"high": "#FF2A6D", "medium": "#FFB703", "low": "#94A3B8"}


def alert_html(alert: dict, case: dict) -> str:
    e = html.escape
    link = f"{os.environ['FRONTEND_URL'].rstrip('/')}/cases/{alert['case_id']}"
    pj = (case.get("primary_jurisdiction") or {}).get("code") or "unassigned"
    icg = case.get("icg") or alert.get("icg")
    icg_line = f"<tr><td style=\"padding-top:8px;font-size:12px;color:#FFB703\">Routed to: {e(icg['name'])} — {e(icg['region'])} (HQ {e(icg['region_hq'])}) · {e(icg['note'])}</td></tr>" if icg else ""
    return f"""
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0A0E17;padding:32px;font-family:Arial,sans-serif;color:#F8FAFC"><tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#162032;border:1px solid #334155;border-radius:8px;padding:28px">
<tr><td style="font-size:20px;font-weight:bold">Varuna<span style="color:#00F0FF">Netra</span> — alert <span style="color:{SEV_COLOR.get(alert['severity'], '#FFB703')};font-size:12px;text-transform:uppercase;letter-spacing:.1em">{e(alert['severity'])} · {e(alert.get('kind', 'high_confidence'))}</span></td></tr>
<tr><td style="padding-top:14px;font-size:14px;line-height:20px;color:#CBD5E1">{e(alert['message'])}</td></tr>
<tr><td style="padding-top:12px;font-size:12px;color:#94A3B8">Case {e(case['case_number'])} · acquired {case['acquisition_time'].strftime('%Y-%m-%d %H:%MZ')} · jurisdiction {e(pj)} · attribution {e(case.get('attribution_status', ''))}</td></tr>
{icg_line}
<tr><td style="padding-top:20px"><a href="{link}" style="background:#00F0FF;color:#0A0E17;padding:10px 18px;border-radius:4px;text-decoration:none;font-weight:bold;font-size:13px">Open case</a></td></tr>
<tr><td style="padding-top:18px;font-size:11px;color:#94A3B8">Decision-support notification. Correlation output indicates possible association only — not a legal finding. Opt out under Users → alert emails.</td></tr>
</table></td></tr></table>"""


async def recipients_for_alerts(icg_code: str = None) -> list:
    """District desk recipients (ICG) first, then supervisors/admins, then admin-configured extra recipients."""
    cfg = await get_config()
    users = await db.users.find({"active": True, "role": {"$in": ["analyst", "supervisor", "admin"]}, "notify_alerts": {"$ne": False}}, {"_id": 0, "email": 1}).to_list(500)
    district = []
    if icg_code:
        d = await db.icg_districts.find_one({"code": icg_code}, {"_id": 0, "recipients": 1})
        district = list((d or {}).get("recipients") or [])
    seen, out = set(), []
    for em in district + [u["email"] for u in users] + list(cfg.get("alert_recipients") or []):
        em = em.lower().strip()
        if em and em not in seen:
            seen.add(em)
            out.append(em)
    return out


async def notify_alert(alert: dict, case: dict) -> dict:
    """Email supervisors/admins about an alert. Never raises; records outcome on the alert."""
    cfg = await get_config()
    now = datetime.now(timezone.utc)
    to = await recipients_for_alerts(((case.get("icg") or alert.get("icg")) or {}).get("code"))
    subject = f"[Varuna Netra] {alert['severity'].upper()} alert — {case['case_number']}" + (f" · {case['icg']['code']}" if case.get("icg") else "")
    if not cfg["api_key"] or not cfg["enabled"] or not cfg.get("alerts_enabled", True):
        summary = {"status": "not_configured", "recipients": to, "sent": 0, "failed": 0, "at": now, "reason": "email delivery not configured or alert emails disabled"}
    elif not to:
        summary = {"status": "no_recipients", "recipients": [], "sent": 0, "failed": 0, "at": now}
    else:
        body = alert_html(alert, case)
        results = []
        for em in to:
            r = await send_email(em, subject, body)
            results.append({"to": em, **r})
        sent = sum(1 for r in results if r["sent"])
        summary = {"status": "sent" if sent == len(to) else ("partial" if sent else "failed"), "recipients": to, "sent": sent, "failed": len(to) - sent, "at": now,
                   "errors": [r["error"] for r in results if not r["sent"]][:5]}
    await db.alerts.update_one({"id": alert["id"]}, {"$set": {"notification": summary}})
    await db.notifications.insert_one({"id": alert["id"] + ":email", "alert_id": alert["id"], "case_id": case["id"], "subject": subject, **summary})
    await audit("alert", alert["id"], "alert.notified", {k: v for k, v in summary.items() if k != "at"}, "system")
    publish("alert", {"alert": {k: v for k, v in alert.items() if k != "_id"}, "notification": {"status": summary["status"], "sent": summary["sent"]}})
    return summary
