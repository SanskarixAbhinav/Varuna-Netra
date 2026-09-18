"""Iteration 30 — SIH stabilization pass.

Covers: AIS status/coverage (STANDBY, no reconnect loop), reference case pin,
dashboard summary breakdown & origin labels, correlation state labels,
analyze-eligible RBAC/idempotency, evidence timeline, Canada EEZ,
plus regression on auth/CORS/health.

The reference case (252e536c-1d84-4f65-9b23-5a8836d9f57b) MUST be left pinned.
Do NOT print secrets. Read-only apart from pin/unpin/pin cycle and one batch job.
"""

import os
import time

import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")
load_dotenv(os.path.join(os.path.dirname(__file__), ".env.test"))

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
REF_ID = "252e536c-1d84-4f65-9b23-5a8836d9f57b"
REF_CN = "SPL-20260910-106"

ADMIN = ("shawpriyanshu950@gmail.com", os.environ["TEST_ADMIN_PASSWORD"])
SUPER = ("supervisor@sentinelmar.demo", os.environ["TEST_SUPERVISOR_PASSWORD"])
ANALY = ("analyst@sentinelmar.demo", os.environ["TEST_ANALYST_PASSWORD"])


def _login(creds):
    r = requests.post(f"{BASE}/api/auth/login", json={"email": creds[0], "password": creds[1]}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def admin_tok():
    return _login(ADMIN)


@pytest.fixture(scope="module")
def super_tok():
    return _login(SUPER)


@pytest.fixture(scope="module")
def analyst_tok():
    return _login(ANALY)


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


# ---------- AIS status / coverage ----------
class TestAisStandby:
    def test_ais_status_standby(self, admin_tok):
        r = requests.get(f"{BASE}/api/ais/status", headers=_h(admin_tok), timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["state"] == "STANDBY", d
        assert d["ingest_enabled"] is False
        assert d["environment"] == "preview"
        assert d["key_configured"] is True
        assert d["worker_role"] == "disabled"
        assert d["websocket_open"] is False
        assert "AIS_INGEST_ENABLED=false" in d.get("reason", "")
        for f in ("feed_state", "socket_owner", "selected_aoi", "messages_per_minute",
                  "active_vessels", "regional_messages", "last_position_at",
                  "last_close_code", "reconnect_count"):
            assert f in d, f"missing field {f}"
        # No key leakage
        blob = r.text.lower()
        for tok in ("aisstream_api_key", "apikey", "api_key\":\""):
            assert tok not in blob or "key_configured" in blob  # allow the boolean field
        # And explicitly ensure the key value from env is not in body (best-effort)
        key = os.environ.get("AISSTREAM_API_KEY", "")
        if key and len(key) > 8:
            assert key not in r.text

    def test_ais_coverage_check(self, admin_tok):
        r = requests.get(f"{BASE}/api/ais/coverage/check", headers=_h(admin_tok), timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "aoi" in d
        assert isinstance(d["recent_positions_in_aoi"], int)
        assert d["feed_state"] == "STANDBY"
        assert d["feed_operational"] is False
        assert d["prompt"] is False
        assert isinstance(d["live_regions"], list)
        assert isinstance(d["message"], str) and d["message"]

    def test_no_reconnect_loop(self, admin_tok):
        r1 = requests.get(f"{BASE}/api/ais/status", headers=_h(admin_tok), timeout=15).json()
        time.sleep(15)
        r2 = requests.get(f"{BASE}/api/ais/status", headers=_h(admin_tok), timeout=15).json()
        assert r1["reconnect_count"] == r2["reconnect_count"], (r1["reconnect_count"], r2["reconnect_count"])
        assert r2["websocket_open"] is False


# ---------- Reference pin ----------
class TestReferencePin:
    def test_reference_get_pinned(self, analyst_tok):
        r = requests.get(f"{BASE}/api/demo/reference", headers=_h(analyst_tok), timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["pinned"] is True
        assert d["available"] is True
        assert "REFERENCE CASE" in d["state"] and "STORED DATA" in d["state"]
        assert d["case_number"] == REF_CN

    def test_pin_forbidden_analyst(self, analyst_tok):
        r = requests.put(f"{BASE}/api/demo/reference/{REF_ID}", headers=_h(analyst_tok), timeout=15)
        assert r.status_code == 403

    def test_pin_nonexistent_admin(self, admin_tok):
        r = requests.put(f"{BASE}/api/demo/reference/00000000-0000-0000-0000-000000000000", headers=_h(admin_tok), timeout=15)
        assert r.status_code == 404

    def test_repin_same_id(self, admin_tok):
        r = requests.put(f"{BASE}/api/demo/reference/{REF_ID}", headers=_h(admin_tok), timeout=15)
        assert r.status_code == 200

    def test_unpin_then_repin(self, admin_tok):
        try:
            d = requests.delete(f"{BASE}/api/demo/reference", headers=_h(admin_tok), timeout=15)
            assert d.status_code in (200, 204), d.text
            g = requests.get(f"{BASE}/api/demo/reference", headers=_h(admin_tok), timeout=15).json()
            assert g["pinned"] is False
            assert "NOT PINNED" in g["state"]
        finally:
            # ALWAYS restore
            rp = requests.put(f"{BASE}/api/demo/reference/{REF_ID}", headers=_h(admin_tok), timeout=15)
            assert rp.status_code == 200, rp.text
            g2 = requests.get(f"{BASE}/api/demo/reference", headers=_h(admin_tok), timeout=15).json()
            assert g2["pinned"] is True and g2["case_number"] == REF_CN


# ---------- Dashboard summary ----------
class TestDashboardSummary:
    def test_summary_shape(self, admin_tok):
        r = requests.get(f"{BASE}/api/dashboard/summary", headers=_h(admin_tok), timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["active_cases"] == 107, d.get("active_cases")
        assert d["live_cases"] == 107
        assert d["pending_review"] == 107
        b = d["breakdown"]
        for k in ("total_records", "active_real", "closed_real", "pending_review",
                  "probable", "possible", "indeterminate", "insufficient_evidence",
                  "analyst_confirmed", "imported", "varuna_detected", "analyst_created", "demo"):
            assert k in b, f"missing breakdown.{k}"
        assert b["total_records"] == 197
        assert b["imported"] == 85
        assert b["varuna_detected"] == 111
        assert b["analyst_created"] == 1
        assert b["demo"] == 0

    def test_summary_matches_cases(self, admin_tok):
        r = requests.get(f"{BASE}/api/cases?origin=real&limit=1000", headers=_h(admin_tok), timeout=20)
        assert r.status_code == 200
        rows = r.json()
        open_rows = [c for c in rows if c.get("status") == "open"]
        pending = [c for c in open_rows if c.get("review_state") == "pending"]
        s = requests.get(f"{BASE}/api/dashboard/summary", headers=_h(admin_tok), timeout=20).json()
        assert len(open_rows) == s["active_cases"], (len(open_rows), s["active_cases"])
        assert len(pending) == s["pending_review"], (len(pending), s["pending_review"])


# ---------- Origin & correlation state labels ----------
class TestCaseLabels:
    def test_real_origin_labels(self, admin_tok):
        rows = requests.get(f"{BASE}/api/cases?origin=real&limit=1000", headers=_h(admin_tok), timeout=20).json()
        assert rows
        allowed_origin = {"VARUNA DETECTED", "ANALYST CREATED"}
        allowed_ds = {"STORED DATA"}
        for c in rows:
            assert c["origin_label"] in allowed_origin, (c.get("case_number"), c.get("origin_label"))
            assert c["data_state"] in allowed_ds, (c.get("case_number"), c.get("data_state"))
        # Correlation state labels
        found_not_analyzable = 0
        found_no_ais = 0
        ref = None
        for c in rows:
            cs = c.get("correlation_state")
            lbl = c.get("correlation_state_label", "")
            if cs == "NOT_ANALYZABLE":
                assert lbl == "NOT ANALYZABLE — HISTORICAL AIS UNAVAILABLE FOR TIME WINDOW", (c.get("case_number"), lbl)
                found_not_analyzable += 1
            if cs == "NO_AIS_COVERAGE":
                assert lbl == "AIS COVERAGE UNAVAILABLE", (c.get("case_number"), lbl)
                found_no_ais += 1
            if c.get("case_number") == REF_CN:
                ref = c
        assert found_not_analyzable > 0, "expected many NOT_ANALYZABLE rows"
        assert ref is not None and ref.get("correlation_state") == "SCORED", ref

    def test_imported_labels(self, admin_tok):
        rows = requests.get(f"{BASE}/api/cases?origin=imported&limit=1000", headers=_h(admin_tok), timeout=20).json()
        assert rows
        for c in rows:
            assert c["origin_label"] == "IMPORTED HISTORICAL", (c.get("case_number"), c.get("origin_label"))
            assert c["data_state"] == "HISTORICAL DATA", (c.get("case_number"), c.get("data_state"))


# ---------- Analyze eligible ----------
class TestAnalyzeEligible:
    def test_forbidden_analyst(self, analyst_tok):
        r = requests.post(f"{BASE}/api/cases/analyze-eligible", headers=_h(analyst_tok), timeout=20)
        assert r.status_code == 403

    def test_supervisor_202_idempotent(self, super_tok):
        r1 = requests.post(f"{BASE}/api/cases/analyze-eligible", headers=_h(super_tok), timeout=30)
        assert r1.status_code == 202, r1.text
        d1 = r1.json()
        for k in ("candidates_checked", "queued", "skipped"):
            assert k in d1
        assert isinstance(d1["queued"], list)
        assert isinstance(d1["skipped"], list)
        for s in d1["skipped"]:
            assert "case_number" in s and "reason" in s
        # 2nd call: idempotent
        r2 = requests.post(f"{BASE}/api/cases/analyze-eligible", headers=_h(super_tok), timeout=30)
        assert r2.status_code == 202
        d2 = r2.json()
        assert d2["queued"] == d1["queued"], (d1["queued"], d2["queued"])
        # Ref case correlation_result version unchanged (was 1 before)
        cr = requests.get(f"{BASE}/api/cases/{REF_ID}/correlation-results", headers=_h(super_tok), timeout=20)
        if cr.status_code == 200:
            results = cr.json()
            if isinstance(results, list) and results:
                versions = [r.get("version") for r in results]
                assert max(v for v in versions if v is not None) == 1, versions


# ---------- Evidence timeline ----------
class TestEvidenceTimeline:
    def test_ref_timeline(self, analyst_tok):
        r = requests.get(f"{BASE}/api/cases/{REF_ID}/evidence-timeline", headers=_h(analyst_tok), timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        events = d.get("events") or []
        assert events, "expected non-empty events"
        kinds = {e.get("kind") for e in events}
        for k in ("scene_acquisition", "detection", "corridor_entry", "closest_approach",
                  "corridor_exit", "correlation", "case_created"):
            assert k in kinds, f"missing kind {k} in {kinds}"
        # Sorted ascending & ISO timestamps
        times = [e.get("time") for e in events]
        assert times == sorted(times), times
        for t in times:
            assert isinstance(t, str) and ("T" in t), t
        # Unavailable includes Analyst review
        un = d.get("unavailable") or []
        joined = " | ".join((u.get("label", "") + " " + u.get("state", "")) for u in un) if un else ""
        assert "Analyst review" in joined, un
        note = d.get("note") or ""
        assert "stored" in note.lower() or "record" in note.lower(), note

    def test_not_analyzed_timeline(self, analyst_tok, admin_tok):
        # find a NOT_ANALYZED (or NOT_ANALYZABLE) case
        rows = requests.get(f"{BASE}/api/cases?origin=real&limit=1000", headers=_h(admin_tok), timeout=20).json()
        target = next((c for c in rows if c.get("correlation_state") in ("NOT_ANALYZED", "NOT_ANALYZABLE")), None)
        assert target is not None, "no NOT_ANALYZED/NOT_ANALYZABLE case found"
        r = requests.get(f"{BASE}/api/cases/{target['id']}/evidence-timeline", headers=_h(analyst_tok), timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        un = d.get("unavailable") or []
        joined = " | ".join((u.get("label", "") + " " + u.get("state", "")) for u in un)
        # Should mention corridor or closest approach as unavailable
        assert ("corridor" in joined.lower() or "closest" in joined.lower()), un
        assert "not available" in joined.lower() or "unavailable" in joined.lower(), un


# ---------- Canada EEZ ----------
class TestCanadaEEZ:
    def test_can_eez(self, analyst_tok):
        r = requests.get(f"{BASE}/api/jurisdictions?country=CAN", headers=_h(analyst_tok), timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        zones = d.get("zones") or d.get("items") or (d if isinstance(d, list) else [])
        zone = next((z for z in zones if z.get("code") == "CAN-EEZ" or z.get("zone_id") == "CAN-EEZ"), None)
        assert zone is not None, zones[:2] if zones else d
        assert zone.get("provenance") == "REFERENCE"
        assert "Marine Regions" in (zone.get("source") or "")
        v = zone.get("vertices") or zone.get("vertex_count") or 0
        assert (isinstance(v, int) and v > 0) or (isinstance(v, list) and len(v) > 0), zone

    def test_lookup(self, analyst_tok):
        r = requests.post(f"{BASE}/api/jurisdictions/lookup", json={"lat": 47, "lon": -60}, headers=_h(analyst_tok), timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("inside_zone") is True, d
        primary = d.get("primary") or (d.get("zones") or [{}])[0]
        zid = primary.get("code") or primary.get("zone_id")
        assert zid == "CAN-EEZ", d

    def test_total_count(self, analyst_tok):
        r = requests.get(f"{BASE}/api/jurisdictions?limit=1", headers=_h(analyst_tok), timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        total = d.get("total") if isinstance(d, dict) else None
        assert total is not None and total >= 287, d


# ---------- Regression ----------
class TestRegression:
    def test_capabilities_google_ready(self):
        r = requests.get(f"{BASE}/api/auth/capabilities", timeout=15)
        assert r.status_code == 200
        d = r.json()
        g = d.get("authentication", {}).get("google", {})
        assert g.get("status") == "READY", g

    def test_admin_login_ok(self):
        r = requests.post(f"{BASE}/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]}, timeout=15)
        assert r.status_code == 200

    def test_wrong_password_401(self):
        r = requests.post(f"{BASE}/api/auth/login", json={"email": ADMIN[0], "password": "wrong-nope"}, timeout=15)
        assert r.status_code == 401

    def test_users_unauth(self):
        r = requests.get(f"{BASE}/api/users", timeout=15)
        assert r.status_code == 401

    def test_cors_foreign_origin(self):
        r = requests.options(
            f"{BASE}/api/auth/login",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
            timeout=15,
        )
        assert r.status_code == 400, r.status_code

    def test_health_auth_no_secrets(self):
        r = requests.get(f"{BASE}/api/health", timeout=15)
        assert r.status_code == 200
        blob = r.text
        auth = r.json().get("authentication")
        assert auth is not None, r.json()
        key = os.environ.get("AISSTREAM_API_KEY", "")
        if key and len(key) > 8:
            assert key not in blob
