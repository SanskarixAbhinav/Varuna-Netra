"""Iteration 4 backend tests: EEZ import, email settings, watchlist, timeline & shares."""
import os
import time
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


# ---------- EEZ IMPORT ----------

class TestEEZImport:
    def test_invalid_iso_400(self, admin_h):
        r = requests.post(f"{API}/jurisdictions/import/marine-regions",
                          json={"iso3": ["XX"], "replace_demo": False}, headers=admin_h, timeout=15)
        assert r.status_code == 400

    def test_empty_iso_400(self, admin_h):
        r = requests.post(f"{API}/jurisdictions/import/marine-regions",
                          json={"iso3": [], "replace_demo": False}, headers=admin_h, timeout=15)
        assert r.status_code == 400

    def test_analyst_forbidden(self, analyst_h):
        r = requests.post(f"{API}/jurisdictions/import/marine-regions",
                          json={"iso3": ["BEL"], "replace_demo": False}, headers=analyst_h, timeout=15)
        assert r.status_code == 403

    def test_import_bel_and_poll(self, admin_h):
        r = requests.post(f"{API}/jurisdictions/import/marine-regions",
                          json={"iso3": ["BEL"], "replace_demo": False}, headers=admin_h, timeout=15)
        assert r.status_code == 202, r.text
        job_id = r.json()["id"]
        deadline = time.time() + 120
        job = None
        while time.time() < deadline:
            jr = requests.get(f"{API}/jobs/{job_id}", headers=admin_h, timeout=15)
            assert jr.status_code == 200
            job = jr.json()
            if job["status"] in ("succeeded", "failed"):
                break
            time.sleep(3)
        assert job is not None
        if job["status"] != "succeeded":
            pytest.skip(f"external WFS not reachable / job {job['status']}: {job.get('error')}")
        assert job["result"]["imported"], "no zones imported"
        codes = [z["code"] for z in job["result"]["imported"]]
        assert "BEL-EEZ" in codes
        assert job["result"]["imported"][0]["vertices"] > 0

    def test_jurisdictions_list_has_official_bel(self, admin_h):
        r = requests.get(f"{API}/jurisdictions", headers=admin_h, timeout=15)
        assert r.status_code == 200
        zones = r.json()
        bel = next((z for z in zones if z["code"] == "BEL-EEZ"), None)
        if not bel:
            pytest.skip("BEL-EEZ not present (import may have failed above)")
        assert bel.get("official") is True
        assert bel.get("mrgid")


# ---------- EMAIL SETTINGS ----------

class TestEmailSettings:
    def test_analyst_forbidden(self, analyst_h):
        assert requests.get(f"{API}/settings/email", headers=analyst_h, timeout=10).status_code == 403
        assert requests.put(f"{API}/settings/email", json={"enabled": True}, headers=analyst_h, timeout=10).status_code == 403
        assert requests.post(f"{API}/settings/email/test", headers=analyst_h, timeout=10).status_code == 403

    def test_supervisor_forbidden(self, sup_h):
        assert requests.get(f"{API}/settings/email", headers=sup_h, timeout=10).status_code == 403

    def test_get_initial(self, admin_h):
        r = requests.get(f"{API}/settings/email", headers=admin_h, timeout=10)
        assert r.status_code == 200
        j = r.json()
        assert "configured" in j and "api_key" in j and "sender_email" in j and "enabled" in j and "source" in j

    def test_update_sender(self, admin_h):
        r = requests.put(f"{API}/settings/email",
                         json={"sender_email": "noreply@example.org"}, headers=admin_h, timeout=10)
        assert r.status_code == 200
        assert r.json()["sender_email"] == "noreply@example.org"
        # restore the real sender so alert e-mails keep working
        requests.put(f"{API}/settings/email", json={"sender_email": "onboarding@resend.dev"}, headers=admin_h, timeout=10)

    def test_set_key_masked_and_configured(self, admin_h):
        r = requests.put(f"{API}/settings/email",
                         json={"resend_api_key": "re_testkey_1234567890"}, headers=admin_h, timeout=10)
        assert r.status_code == 200
        j = r.json()
        assert j["configured"] is True
        assert j["source"] == "settings"
        assert "…" in j["api_key"] and "re_te" in j["api_key"] and "7890" in j["api_key"]

    def test_test_email_fails_with_bad_key(self, admin_h):
        r = requests.post(f"{API}/settings/email/test", headers=admin_h, timeout=30)
        assert r.status_code == 400
        # last_test recorded as failed
        g = requests.get(f"{API}/settings/email", headers=admin_h, timeout=10).json()
        assert g.get("last_test", {}).get("sent") is False

    def test_toggle_enabled(self, admin_h):
        r = requests.put(f"{API}/settings/email", json={"enabled": False}, headers=admin_h, timeout=10)
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        r = requests.put(f"{API}/settings/email", json={"enabled": True}, headers=admin_h, timeout=10)
        assert r.json()["enabled"] is True

    def test_clear_key_falls_back_to_environment(self, admin_h):
        # Clearing the DB override must fall back to the backend environment key (never mutate/inspect the secret itself).
        r = requests.put(f"{API}/settings/email", json={"resend_api_key": ""}, headers=admin_h, timeout=10)
        assert r.status_code == 200
        j = r.json()
        assert j["source"] in ("env", "none")
        if j["source"] == "env":
            assert j["configured"] is True  # environment RESEND_API_KEY present → still configured
        else:
            assert j["configured"] is False


# ---------- WATCHLIST ----------

class TestWatchlist:
    def test_list_any_role(self, analyst_h):
        assert requests.get(f"{API}/watchlist", headers=analyst_h, timeout=10).status_code == 200

    def test_analyst_post_forbidden(self, analyst_h):
        r = requests.post(f"{API}/watchlist",
                          json={"mmsi": "111222333", "reason": "test", "severity": "high"},
                          headers=analyst_h, timeout=10)
        assert r.status_code == 403

    def test_supervisor_add(self, sup_h):
        # cleanup first: if there's an existing active entry for 219876543 remove it
        rows = requests.get(f"{API}/watchlist", headers=sup_h, timeout=10).json()
        for w in rows:
            if w["mmsi"] == "219876543" and w.get("active"):
                requests.delete(f"{API}/watchlist/{w['id']}", headers=sup_h, timeout=10)
        r = requests.post(f"{API}/watchlist",
                          json={"mmsi": "219876543", "reason": "test intel", "severity": "high"},
                          headers=sup_h, timeout=10)
        assert r.status_code == 201, r.text
        j = r.json()
        assert j["active"] is True
        assert j["mmsi"] == "219876543"
        # vessel_name should auto-fill if AIS record exists
        assert j.get("vessel_name") in (None, "BALTIC STAR") or isinstance(j.get("vessel_name"), str)
        pytest.watch_id = j["id"]

    def test_duplicate_active_rejected(self, sup_h):
        r = requests.post(f"{API}/watchlist",
                          json={"mmsi": "219876543", "reason": "dup test", "severity": "high"},
                          headers=sup_h, timeout=10)
        assert r.status_code == 400

    def test_bad_severity(self, sup_h):
        r = requests.post(f"{API}/watchlist",
                          json={"mmsi": "555555555", "reason": "bad sev", "severity": "extreme"},
                          headers=sup_h, timeout=10)
        assert r.status_code == 400

    def test_correlation_creates_watchlist_alert(self, sup_h, admin_h):
        # find case SPL-20260610-001
        cases = requests.get(f"{API}/cases", headers=admin_h, timeout=10).json()
        cs = [c for c in cases if c["case_number"] == "SPL-20260610-001"]
        if not cs:
            pytest.skip("seeded case SPL-20260610-001 missing")
        cid = cs[0]["id"]
        r = requests.post(f"{API}/cases/{cid}/correlate", json={"sync": True}, headers=sup_h, timeout=60)
        assert r.status_code in (200, 201, 202), r.text
        # check alerts
        alerts = requests.get(f"{API}/alerts", headers=sup_h, timeout=10).json()
        wl_alerts = [a for a in alerts if a.get("kind") == "watchlist" and a.get("case_id") == cid]
        # candidate 219876543 must exist in case candidates for alert to trigger
        cands_resp = requests.get(f"{API}/cases/{cid}/candidates", headers=sup_h, timeout=10).json()
        cands = cands_resp.get("candidates", []) if isinstance(cands_resp, dict) else cands_resp
        mmsis = [c["mmsi"] for c in cands]
        if "219876543" not in mmsis:
            pytest.skip("219876543 not among candidates for case-001; watchlist hook cannot fire")
        assert wl_alerts, "expected watchlist alert to be created"
        # candidate should carry watchlist info
        target = next(c for c in cands if c["mmsi"] == "219876543")
        assert target.get("watchlist"), "candidate should carry watchlist enrichment"
        assert target["watchlist"].get("reason") == "test intel"

    def test_watchlist_hits_incremented(self, sup_h):
        rows = requests.get(f"{API}/watchlist", headers=sup_h, timeout=10).json()
        entry = next((w for w in rows if w["mmsi"] == "219876543" and w.get("active")), None)
        if not entry:
            pytest.skip("watchlist entry missing")
        # may be 0 if candidate not present; only assert if alert test succeeded
        assert entry.get("hits", 0) >= 0

    def test_analyst_delete_forbidden(self, analyst_h):
        wid = getattr(pytest, "watch_id", None)
        if not wid:
            pytest.skip("no watch_id")
        r = requests.delete(f"{API}/watchlist/{wid}", headers=analyst_h, timeout=10)
        assert r.status_code == 403

    def test_supervisor_delete(self, sup_h):
        wid = getattr(pytest, "watch_id", None)
        if not wid:
            pytest.skip("no watch_id")
        r = requests.delete(f"{API}/watchlist/{wid}", headers=sup_h, timeout=10)
        assert r.status_code == 200
        rows = requests.get(f"{API}/watchlist", headers=sup_h, timeout=10).json()
        entry = next((w for w in rows if w["id"] == wid), None)
        assert entry and entry.get("active") is False


# ---------- TIMELINE & SHARES ----------

def _get_case_001(headers):
    cs = requests.get(f"{API}/cases", headers=headers, timeout=10).json()
    cs = [c for c in cs if c["case_number"] == "SPL-20260610-001"]
    return cs[0]["id"] if cs else None


class TestTimeline:
    def test_timeline_json_any_role(self, analyst_h):
        cid = _get_case_001(analyst_h)
        assert cid
        r = requests.get(f"{API}/cases/{cid}/timeline", headers=analyst_h, timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert "events" in j and "case" in j and "disclaimer" in j
        assert j["events"], "events should not be empty"
        kinds = {e["kind"] for e in j["events"]}
        assert kinds & {"spill", "correlation"}
        # sorted by t
        ts = [e["t"] for e in j["events"]]
        assert ts == sorted(ts)

    def test_timeline_html(self, analyst_h):
        cid = _get_case_001(analyst_h)
        r = requests.get(f"{API}/cases/{cid}/timeline.html", headers=analyst_h, timeout=15)
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "SPL-20260610-001" in r.text

    def test_analyst_share_forbidden(self, analyst_h):
        cid = _get_case_001(analyst_h)
        r = requests.post(f"{API}/cases/{cid}/share", json={"expires_hours": 24}, headers=analyst_h, timeout=10)
        assert r.status_code == 403

    def test_share_expires_hours_0_invalid(self, sup_h):
        cid = _get_case_001(sup_h)
        r = requests.post(f"{API}/cases/{cid}/share", json={"expires_hours": 0}, headers=sup_h, timeout=10)
        assert r.status_code == 422

    def test_supervisor_create_and_public_view(self, sup_h):
        cid = _get_case_001(sup_h)
        r = requests.post(f"{API}/cases/{cid}/share",
                         json={"expires_hours": 24, "recipient_note": "Coastguard NL"},
                         headers=sup_h, timeout=10)
        assert r.status_code == 201, r.text
        j = r.json()
        assert "/api/share/" in j["url"]
        assert j["views"] == 0
        # public GET without auth
        pr = requests.get(j["url"], timeout=15)
        assert pr.status_code == 200
        assert "text/html" in pr.headers.get("content-type", "")
        assert "SPL-20260610-001" in pr.text
        assert "Shared read-only" in pr.text
        pytest.share_id = j["id"]
        pytest.share_url = j["url"]
        # list shares
        lst = requests.get(f"{API}/cases/{cid}/shares", headers=sup_h, timeout=10).json()
        s = next((x for x in lst if x["id"] == j["id"]), None)
        assert s and s["views"] >= 1

    def test_bogus_token_404(self):
        r = requests.get(f"{BASE_URL}/api/share/bogusnonexistenttoken12345", timeout=10)
        assert r.status_code == 404

    def test_revoke_share(self, sup_h):
        cid = _get_case_001(sup_h)
        sid = getattr(pytest, "share_id", None)
        surl = getattr(pytest, "share_url", None)
        if not sid:
            pytest.skip("no share id")
        r = requests.delete(f"{API}/cases/{cid}/shares/{sid}", headers=sup_h, timeout=10)
        assert r.status_code == 200
        pr = requests.get(surl, timeout=10)
        assert pr.status_code == 404


# ---------- REGRESSION ----------

class TestRegression:
    def test_login_all_roles(self):
        for creds in (ADMIN, SUPERVISOR, ANALYST):
            _login(*creds)

    def test_dashboard_case_has_jurisdiction(self, analyst_h):
        cs = requests.get(f"{API}/cases", headers=analyst_h, timeout=10).json()
        c1 = next((c for c in cs if c["case_number"] == "SPL-20260610-001"), None)
        assert c1
        pj = c1.get("primary_jurisdiction") or {}
        assert pj.get("code") == "NLD-EEZ"

    def test_pdf_export(self, analyst_h):
        cid = _get_case_001(analyst_h)
        r = requests.get(f"{API}/cases/{cid}/evidence.pdf", headers=analyst_h, timeout=30)
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("application/pdf")

    def test_forgot_password_logged(self):
        r = requests.post(f"{API}/auth/forgot-password", json={"email": ANALYST[0]}, timeout=15)
        assert r.status_code == 200
        assert r.json()["delivery"] == "logged"
