"""Iteration 5 backend tests: Attachments, Notifications, Compare, Zone Rules."""
import io
import os
import struct
import time
import zlib

import pytest
import requests


def _load_backend_url():
    if os.environ.get("REACT_APP_BACKEND_URL"):
        return os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                return line.split("=", 1)[1].strip().strip('"').rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL not found")


BASE_URL = _load_backend_url()
API = f"{BASE_URL}/api"

ADMIN = ("shawpriyanshu950@gmail.com", os.environ["TEST_ADMIN_PASSWORD"])
ANALYST = ("analyst@sentinelmar.demo", os.environ["TEST_ANALYST_PASSWORD"])
SUPERVISOR = ("supervisor@sentinelmar.demo", os.environ["TEST_SUPERVISOR_PASSWORD"])


def _login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login {email} → {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def admin_h():
    return {"Authorization": f"Bearer {_login(*ADMIN)}"}


@pytest.fixture(scope="session")
def sup_h():
    return {"Authorization": f"Bearer {_login(*SUPERVISOR)}"}


@pytest.fixture(scope="session")
def analyst_h():
    return {"Authorization": f"Bearer {_login(*ANALYST)}"}


def _case_id(headers, case_num):
    cs = requests.get(f"{API}/cases", headers=headers, timeout=10).json()
    return next((c["id"] for c in cs if c["case_number"] == case_num), None)


def _make_png(w=4, h=4) -> bytes:
    """Minimal valid PNG bytes."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# ---------- ATTACHMENTS ----------

class TestAttachments:
    def test_init_unsupported_ext(self, analyst_h, admin_h):
        cid = _case_id(admin_h, "SPL-20260610-001")
        r = requests.post(f"{API}/uploads/init", json={"case_id": cid, "filename": "bad.exe", "size": 100, "total_chunks": 1}, headers=analyst_h, timeout=10)
        assert r.status_code == 400

    def test_init_unknown_case(self, analyst_h):
        r = requests.post(f"{API}/uploads/init", json={"case_id": "nonexistent-case-id", "filename": "a.png", "size": 100, "total_chunks": 1}, headers=analyst_h, timeout=10)
        assert r.status_code == 404

    def test_init_oversize_422(self, analyst_h, admin_h):
        cid = _case_id(admin_h, "SPL-20260610-001")
        r = requests.post(f"{API}/uploads/init", json={"case_id": cid, "filename": "big.png", "size": 60 * 1024 * 1024, "total_chunks": 1}, headers=analyst_h, timeout=10)
        assert r.status_code == 422

    def test_full_upload_flow(self, analyst_h, admin_h, sup_h):
        cid = _case_id(admin_h, "SPL-20260610-001")
        png = _make_png()
        # init
        r = requests.post(f"{API}/uploads/init", json={"case_id": cid, "filename": "test_att.png", "size": len(png), "total_chunks": 1}, headers=analyst_h, timeout=10)
        assert r.status_code == 201, r.text
        upload_id = r.json()["id"]
        # chunk
        r = requests.put(f"{API}/uploads/{upload_id}/chunks/0", files={"chunk": ("chunk0", io.BytesIO(png), "application/octet-stream")}, headers=analyst_h, timeout=15)
        assert r.status_code == 200
        assert r.json()["received"] == 1
        # complete
        r = requests.post(f"{API}/uploads/{upload_id}/complete", json={"caption": "test scene", "kind": "sar_scene"}, headers=analyst_h, timeout=30)
        assert r.status_code == 201, r.text
        att = r.json()
        assert att["is_image"] is True
        assert att["storage_path"]
        assert att["kind"] == "sar_scene"
        assert att["caption"] == "test scene"
        pytest.att_id = att["id"]
        pytest.att_cid = cid

    def test_complete_bad_kind(self, analyst_h, admin_h):
        cid = _case_id(admin_h, "SPL-20260610-001")
        png = _make_png()
        r = requests.post(f"{API}/uploads/init", json={"case_id": cid, "filename": "x.png", "size": len(png), "total_chunks": 1}, headers=analyst_h, timeout=10)
        upload_id = r.json()["id"]
        requests.put(f"{API}/uploads/{upload_id}/chunks/0", files={"chunk": ("c", io.BytesIO(png), "application/octet-stream")}, headers=analyst_h, timeout=10)
        r = requests.post(f"{API}/uploads/{upload_id}/complete", json={"kind": "invalid_kind"}, headers=analyst_h, timeout=10)
        assert r.status_code == 400

    def test_complete_missing_chunks(self, analyst_h, admin_h):
        cid = _case_id(admin_h, "SPL-20260610-001")
        r = requests.post(f"{API}/uploads/init", json={"case_id": cid, "filename": "m.png", "size": 200, "total_chunks": 3}, headers=analyst_h, timeout=10)
        upload_id = r.json()["id"]
        # Only upload chunk 0
        requests.put(f"{API}/uploads/{upload_id}/chunks/0", files={"chunk": ("c", io.BytesIO(b"partial"), "application/octet-stream")}, headers=analyst_h, timeout=10)
        r = requests.post(f"{API}/uploads/{upload_id}/complete", json={"kind": "other"}, headers=analyst_h, timeout=10)
        assert r.status_code == 400
        assert "missing" in r.text.lower()

    def test_list_attachments(self, analyst_h):
        cid = getattr(pytest, "att_cid", None)
        att_id = getattr(pytest, "att_id", None)
        if not att_id:
            pytest.skip("no attachment uploaded")
        r = requests.get(f"{API}/cases/{cid}/attachments", headers=analyst_h, timeout=10)
        assert r.status_code == 200
        atts = r.json()
        assert any(a["id"] == att_id for a in atts)

    def test_download(self, analyst_h):
        att_id = getattr(pytest, "att_id", None)
        if not att_id:
            pytest.skip("no attachment")
        r = requests.get(f"{API}/attachments/{att_id}/download", headers=analyst_h, timeout=30)
        assert r.status_code == 200
        assert "image/png" in r.headers.get("content-type", "")
        assert r.content.startswith(b"\x89PNG")

    def test_analyst_delete_forbidden(self, analyst_h):
        att_id = getattr(pytest, "att_id", None)
        if not att_id:
            pytest.skip("no attachment")
        r = requests.delete(f"{API}/attachments/{att_id}", headers=analyst_h, timeout=10)
        assert r.status_code == 403

    def test_evidence_pdf_still_works(self, analyst_h):
        cid = getattr(pytest, "att_cid", None)
        if not cid:
            pytest.skip()
        r = requests.get(f"{API}/cases/{cid}/evidence.pdf", headers=analyst_h, timeout=30)
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("application/pdf")

    def test_timeline_includes_attachments(self, analyst_h):
        cid = getattr(pytest, "att_cid", None)
        att_id = getattr(pytest, "att_id", None)
        if not (cid and att_id):
            pytest.skip()
        r = requests.get(f"{API}/cases/{cid}/timeline", headers=analyst_h, timeout=15)
        assert r.status_code == 200
        j = r.json()
        # Top-level attachments array
        assert "attachments" in j
        assert any(a.get("id") == att_id for a in j["attachments"])
        # Event kind 'attachment'
        att_events = [e for e in j["events"] if e.get("kind") == "attachment"]
        assert att_events, "expected at least one 'attachment' timeline event"

    def test_supervisor_delete(self, sup_h, analyst_h):
        att_id = getattr(pytest, "att_id", None)
        cid = getattr(pytest, "att_cid", None)
        if not att_id:
            pytest.skip()
        r = requests.delete(f"{API}/attachments/{att_id}", headers=sup_h, timeout=10)
        assert r.status_code == 200
        # List should exclude it
        atts = requests.get(f"{API}/cases/{cid}/attachments", headers=analyst_h, timeout=10).json()
        assert not any(a["id"] == att_id for a in atts)
        # Download 404
        r = requests.get(f"{API}/attachments/{att_id}/download", headers=analyst_h, timeout=10)
        assert r.status_code == 404


# ---------- NOTIFICATIONS ----------

class TestNotifications:
    def test_email_settings_alerts_fields(self, admin_h):
        r = requests.get(f"{API}/settings/email", headers=admin_h, timeout=10)
        assert r.status_code == 200
        j = r.json()
        assert "alerts_enabled" in j
        assert "alert_recipients" in j

    def test_put_alert_recipients_normalizes(self, admin_h):
        r = requests.put(f"{API}/settings/email",
                         json={"alert_recipients": ["Foo@Bar.com", "invalid-no-at", "second@example.org"]},
                         headers=admin_h, timeout=10)
        assert r.status_code == 200
        rec = r.json()["alert_recipients"]
        assert "foo@bar.com" in rec
        assert "second@example.org" in rec
        assert "invalid-no-at" not in rec

    def test_toggle_alerts_enabled(self, admin_h):
        r = requests.put(f"{API}/settings/email", json={"alerts_enabled": False}, headers=admin_h, timeout=10)
        assert r.status_code == 200
        assert r.json()["alerts_enabled"] is False
        r = requests.put(f"{API}/settings/email", json={"alerts_enabled": True}, headers=admin_h, timeout=10)
        assert r.json()["alerts_enabled"] is True

    def test_user_notify_alerts_toggle(self, admin_h):
        users = requests.get(f"{API}/users", headers=admin_h, timeout=10).json()
        sup = next(u for u in users if u["email"] == SUPERVISOR[0])
        r = requests.patch(f"{API}/users/{sup['id']}", json={"notify_alerts": False}, headers=admin_h, timeout=10)
        assert r.status_code == 200
        users2 = requests.get(f"{API}/users", headers=admin_h, timeout=10).json()
        sup2 = next(u for u in users2 if u["email"] == SUPERVISOR[0])
        assert sup2.get("notify_alerts") is False
        # Restore
        r = requests.patch(f"{API}/users/{sup['id']}", json={"notify_alerts": True}, headers=admin_h, timeout=10)
        assert r.status_code == 200

    def test_alert_has_notification_not_configured(self, sup_h, admin_h):
        # Trigger via zone-rules/evaluate
        r = requests.post(f"{API}/zone-rules/evaluate", headers=sup_h, timeout=60)
        assert r.status_code == 200
        # Get alerts
        alerts = requests.get(f"{API}/alerts", headers=sup_h, timeout=15).json()
        # Find any alert that has notification field
        notified = [a for a in alerts if a.get("notification")]
        assert notified, "expected at least one alert with notification recorded"
        n = notified[0]["notification"]
        assert n["status"] == "not_configured"
        assert isinstance(n.get("recipients"), list)
        # Should include admin/supervisor emails (unless muted)
        # Include the extra second@example.org added earlier
        # Should include admin/supervisor emails (unless muted)
        assert "supervisor@sentinelmar.demo" in n["recipients"] or "shawpriyanshu950@gmail.com" in n["recipients"]
        assert n["sent"] == 0

    def test_cleanup_alert_recipients(self, admin_h):
        # Restore alert_recipients to empty for cleanliness
        r = requests.put(f"{API}/settings/email", json={"alert_recipients": []}, headers=admin_h, timeout=10)
        assert r.status_code == 200


# ---------- COMPARE ----------

class TestCompare:
    def test_compare_ok(self, analyst_h, admin_h):
        a = _case_id(admin_h, "SPL-20260610-001")
        b = _case_id(admin_h, "SPL-20260610-002")
        assert a and b
        r = requests.get(f"{API}/cases/compare/{a}/{b}", headers=analyst_h, timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        for side in ("a", "b"):
            assert side in j
            assert "case" in j[side]
            assert "geojson" in j[side]
            assert "candidates" in j[side]
        assert "shared_vessels" in j
        assert isinstance(j["shared_vessels"], list)
        assert "disclaimer" in j

    def test_compare_bad_id(self, analyst_h, admin_h):
        a = _case_id(admin_h, "SPL-20260610-001")
        r = requests.get(f"{API}/cases/compare/{a}/nonexistent", headers=analyst_h, timeout=10)
        assert r.status_code == 404


# ---------- ZONE RULES ----------

class TestZoneRules:
    def test_list_any_role(self, analyst_h):
        r = requests.get(f"{API}/zone-rules", headers=analyst_h, timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_analyst_create_forbidden(self, analyst_h):
        r = requests.post(f"{API}/zone-rules",
                          json={"name": "test", "zone_code": "NLD-EEZ", "severity": "high"},
                          headers=analyst_h, timeout=10)
        assert r.status_code == 403

    def test_unknown_zone_400(self, sup_h):
        r = requests.post(f"{API}/zone-rules",
                          json={"name": "bad zone rule", "zone_code": "ZZZ-NOPE", "severity": "high"},
                          headers=sup_h, timeout=10)
        assert r.status_code == 400

    def test_bad_severity_400(self, sup_h):
        r = requests.post(f"{API}/zone-rules",
                          json={"name": "bad sev rule", "zone_code": "NLD-EEZ", "severity": "extreme"},
                          headers=sup_h, timeout=10)
        assert r.status_code == 400

    def test_supervisor_create(self, sup_h):
        r = requests.post(f"{API}/zone-rules",
                         json={"name": "TEST iter5 rule", "zone_code": "NLD-EEZ",
                               "min_area_km2": 0.01, "min_confidence": 0.0, "severity": "medium"},
                         headers=sup_h, timeout=10)
        assert r.status_code == 201, r.text
        j = r.json()
        assert j["active"] is True
        assert j["severity"] == "medium"
        pytest.rule_id = j["id"]

    def test_evaluate_raises_once(self, sup_h):
        rid = getattr(pytest, "rule_id", None)
        if not rid:
            pytest.skip()
        r1 = requests.post(f"{API}/zone-rules/evaluate", headers=sup_h, timeout=60)
        assert r1.status_code == 200
        raised1 = r1.json()["alerts_raised"]
        # Second run: expect 0 new
        r2 = requests.post(f"{API}/zone-rules/evaluate", headers=sup_h, timeout=60)
        assert r2.status_code == 200
        raised2 = r2.json()["alerts_raised"]
        assert raised2 == 0, f"re-run should raise 0 new, got {raised2}"

    def test_patch_severity(self, sup_h):
        rid = getattr(pytest, "rule_id", None)
        if not rid:
            pytest.skip()
        r = requests.patch(f"{API}/zone-rules/{rid}", json={"severity": "low", "active": False}, headers=sup_h, timeout=10)
        assert r.status_code == 200
        assert r.json()["severity"] == "low"
        assert r.json()["active"] is False

    def test_patch_bad_severity(self, sup_h):
        rid = getattr(pytest, "rule_id", None)
        if not rid:
            pytest.skip()
        r = requests.patch(f"{API}/zone-rules/{rid}", json={"severity": "bogus"}, headers=sup_h, timeout=10)
        assert r.status_code == 400

    def test_delete_rule(self, sup_h, analyst_h):
        rid = getattr(pytest, "rule_id", None)
        if not rid:
            pytest.skip()
        # analyst delete forbidden
        r = requests.delete(f"{API}/zone-rules/{rid}", headers=analyst_h, timeout=10)
        assert r.status_code == 403
        # supervisor deletes
        r = requests.delete(f"{API}/zone-rules/{rid}", headers=sup_h, timeout=10)
        assert r.status_code == 200
