import asyncio
import logging
import os
from datetime import datetime, timezone

import resend

from db import db

logger = logging.getLogger("emailer")


async def get_config() -> dict:
    """DB-stored settings (admin UI) take precedence over environment."""
    s = await db.settings.find_one({"key": "email"}, {"_id": 0}) or {}
    return {
        "api_key": s.get("resend_api_key") or os.environ.get("RESEND_API_KEY") or "",
        "sender_email": s.get("sender_email") or os.environ.get("SENDER_EMAIL") or "onboarding@resend.dev",
        "enabled": s.get("enabled", True),
        "alerts_enabled": s.get("alerts_enabled", True),
        "alert_recipients": s.get("alert_recipients") or [],
        "source": "settings" if s.get("resend_api_key") else ("env" if os.environ.get("RESEND_API_KEY") else "none"),
        "updated_at": s.get("updated_at"), "updated_by": s.get("updated_by"), "last_test": s.get("last_test"),
    }


async def configured() -> bool:
    c = await get_config()
    return bool(c["api_key"]) and c["enabled"]


async def send_email(to: str, subject: str, html: str) -> dict:
    """Returns {sent: bool, id|error}. Never raises."""
    c = await get_config()
    if not c["api_key"] or not c["enabled"]:
        logger.warning("email not configured — to %s NOT sent (subject: %s)", to, subject)
        return {"sent": False, "error": "email delivery not configured"}
    resend.api_key = c["api_key"]
    params = {"from": c["sender_email"], "to": [to], "subject": subject, "html": html}
    try:
        res = await asyncio.to_thread(resend.Emails.send, params)
        mid = res.get("id") if isinstance(res, dict) else str(res)
        logger.info("email sent to %s id=%s", to, mid)
        return {"sent": True, "id": mid}
    except Exception as e:  # noqa: BLE001
        logger.error("Resend send failed: %s", e)
        return {"sent": False, "error": str(e)[:300]}


async def record_test(result: dict, to: str):
    await db.settings.update_one({"key": "email"}, {"$set": {"last_test": {**result, "to": to, "at": datetime.now(timezone.utc)}}}, upsert=True)


def reset_email_html(name: str, link: str) -> str:
    return f"""
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0A0E17;padding:32px;font-family:Arial,sans-serif;color:#F8FAFC">
  <tr><td align="center">
    <table width="520" cellpadding="0" cellspacing="0" style="background:#162032;border:1px solid #334155;border-radius:8px;padding:28px">
      <tr><td style="font-size:20px;font-weight:bold">Sentinel<span style="color:#00F0FF">Mar</span> — password reset</td></tr>
      <tr><td style="padding-top:14px;font-size:14px;line-height:20px;color:#CBD5E1">Hello {name},<br/>A password reset was requested for your authority account. This link expires in 60 minutes and can be used once.</td></tr>
      <tr><td style="padding-top:20px"><a href="{link}" style="background:#00F0FF;color:#0A0E17;padding:10px 18px;border-radius:4px;text-decoration:none;font-weight:bold;font-size:13px">Reset password</a></td></tr>
      <tr><td style="padding-top:18px;font-size:11px;color:#94A3B8">If you did not request this, ignore this email. The request has been recorded in the audit log.</td></tr>
    </table>
  </td></tr>
</table>"""


def test_email_html(name: str) -> str:
    return f"""<div style="font-family:Arial,sans-serif;padding:24px;background:#0A0E17;color:#F8FAFC"><h2>Sentinel<span style="color:#00F0FF">Mar</span> email delivery test</h2>
<p style="color:#CBD5E1">Hello {name}, Resend delivery is configured correctly. Password-reset and alert emails will be sent from this address.</p></div>"""
