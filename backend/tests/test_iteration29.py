"""Iteration 29 — SIH judge readiness: auth capabilities, Google gating, case provenance labels, correlation states, dashboard cross-check, provenance and candidates factor sums."""
import os
import time
import subprocess
import pytest
import requests

def _load_frontend_env():
    p = "/app/frontend/.env"
    if os.path.exists(p):
        with open(p) as f:
            for ln in f:
                if ln.startswith("REACT_APP_BACKEND_URL="):
                    return ln.split("=", 1)[1].strip()
    return ""

BASE = (os.environ.get("REACT_APP_BACKEND_URL") or _load_frontend_env()).rstrip("/")
ADMIN = ("shawpriyanshu950@gmail.com", os.environ["TEST_ADMIN_PASSWORD"])
ANALYST = ("analyst@sentinelmar.demo", os.environ["TEST_ANALYST_PASSWORD"])

SECRET_KEYWORDS = ("secret", "client_secret", "api_key", "apikey", "private_key")


def _login(email, pw):
    r = requests.post(f"{BASE}/api/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def admin_token():
    return _login(*ADMIN)


@pytest.fixture(scope="module")
def analyst_token():
    return _login(*ANALYST)


def _has_secret(obj):
    import json
    j = json.dumps(obj).lower()
    return any(k in j for k in SECRET_KEYWORDS)


# ---- capabilities & health (public) ----
class TestCapabilities:
    def test_capabilities_public_and_shape(self):
        r = requests.get(f"{BASE}/api/auth/capabilities", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["environment"] == "production"
        assert d["demo_mode"] is False
        auth = d["authentication"]
        assert auth["email_password"] is True
        g = auth["google"]
        assert g["enabled"] is True
        assert g["configured"] is True
        assert g["status"] == "READY"
        assert not _has_secret(d), f"capabilities leaks secret: {d}"

    def test_health_authentication_block(self):
        r = requests.get(f"{BASE}/api/health", timeout=10)
        assert r.status_code == 200
        d = r.json()
        auth = d.get("authentication")
        assert auth is not None
        assert auth["google"]["status"] == "READY"
        assert auth["email_password"] is True
        assert not _has_secret(auth)


# ---- Google session failure & lockout ----
class TestGoogleSessionFailure:
    def test_bogus_session_returns_401(self):
        r = requests.post(f"{BASE}/api/auth/google/session", json={"session_id": "bogus-iter29"}, timeout=15)
        assert r.status_code in (401, 429), r.text
        body = r.text
        assert "Traceback" not in body and "stack" not in body.lower()

    def test_password_login_still_works_after_google_failure(self):
        # ensure another identifier can still login
        tok = _login(*ANALYST)
        assert tok


# ---- email/password login & RBAC ----
class TestAuth:
    def test_admin_login_ok(self, admin_token):
        r = requests.get(f"{BASE}/api/auth/me", headers={"Authorization": f"Bearer {admin_token}"}, timeout=10)
        assert r.status_code == 200
        assert r.json()["email"] == ADMIN[0]

    def test_wrong_password_generic(self):
        r = requests.post(f"{BASE}/api/auth/login", json={"email": ADMIN[0], "password": "wrong-pw-iter29"}, timeout=10)
        assert r.status_code in (401, 429)
        if r.status_code == 401:
            msg = r.json().get("detail", "")
            assert "invalid" in msg.lower() or "incorrect" in msg.lower()

    def test_users_no_token_401(self):
        r = requests.get(f"{BASE}/api/users", timeout=10)
        assert r.status_code == 401

    def test_users_analyst_403(self, analyst_token):
        r = requests.get(f"{BASE}/api/users", headers={"Authorization": f"Bearer {analyst_token}"}, timeout=10)
        assert r.status_code == 403

    def test_users_admin_ok_no_password_hash(self, admin_token):
        r = requests.get(f"{BASE}/api/users", headers={"Authorization": f"Bearer {admin_token}"}, timeout=10)
        assert r.status_code == 200
        users = r.json()
        assert isinstance(users, list) and len(users) > 0
        for u in users:
            assert "password_hash" not in u, f"password_hash leaked in user {u.get('email')}"
            assert "password" not in u


# ---- session persistence across restart (JWT stateless) ----
class TestSessionPersistence:
    def test_token_survives_backend_restart(self, admin_token):
        subprocess.run(["sudo", "supervisorctl", "restart", "backend"], check=False, capture_output=True)
        # wait for backend to come back
        deadline = time.time() + 30
        ok = False
        while time.time() < deadline:
            try:
                h = requests.get(f"{BASE}/api/health", timeout=5)
                if h.status_code == 200:
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(1)
        assert ok, "backend did not come back within 30s"
        r = requests.get(f"{BASE}/api/auth/me", headers={"Authorization": f"Bearer {admin_token}"}, timeout=10)
        assert r.status_code == 200

    def test_logout_ok(self, admin_token):
        r = requests.post(f"{BASE}/api/auth/logout", headers={"Authorization": f"Bearer {admin_token}"}, timeout=10)
        assert r.status_code in (200, 204)


# ---- Cases provenance & correlation ----
CASE_SPL = "252e536c-1d84-4f65-9b23-5a8836d9f57b"


class TestCases:
    def test_cases_real_origin_labels(self, admin_token):
        r = requests.get(f"{BASE}/api/cases", params={"origin": "real", "limit": 1000},
                         headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
        assert r.status_code == 200
        cases = r.json()
        assert isinstance(cases, list) and len(cases) > 0
        allowed_labels = {"LIVE DETECTED", "ANALYST CREATED"}
        allowed_states = {"NOT_ANALYZED", "NO_AIS_COVERAGE", "NO_CANDIDATE_IN_TIME_WINDOW", "SCORED"}
        target = None
        for c in cases:
            assert c.get("origin_label") in allowed_labels, f"{c.get('id')}: origin_label={c.get('origin_label')}"
            assert c.get("correlation_state") in allowed_states, f"{c.get('id')}: state={c.get('correlation_state')}"
            assert c.get("correlation_state_label") is not None
            cs = c.get("detection_confidence_source")
            assert cs in ("detector", "registrant"), f"{c.get('id')}: dcs={cs}"
            if c.get("source") == "dark_spot_detector":
                assert cs == "detector"
            else:
                assert cs == "registrant"
            lrv = c.get("latest_result_version")
            if lrv in (0, None):
                assert c.get("correlation_state") == "NOT_ANALYZED", f"case {c.get('id')} lrv={lrv} state={c.get('correlation_state')}"
            if c.get("id") == CASE_SPL:
                target = c
        assert target is not None, "target SPL-20260910-106 not in real cases"
        assert target["correlation_state"] == "SCORED"
        assert target.get("candidate_count") == 5

    def test_cases_imported(self, admin_token):
        r = requests.get(f"{BASE}/api/cases", params={"origin": "imported", "limit": 1000},
                         headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
        assert r.status_code == 200
        cases = r.json()
        assert len(cases) > 0
        for c in cases:
            assert c.get("origin_label") == "IMPORTED HISTORICAL"

    def test_cases_all_matches_dashboard(self, admin_token):
        r_all = requests.get(f"{BASE}/api/cases", params={"origin": "all", "limit": 1000},
                             headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
        r_summary = requests.get(f"{BASE}/api/dashboard/summary",
                                 headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)
        assert r_all.status_code == 200 and r_summary.status_code == 200
        cases_all = r_all.json()
        summary = r_summary.json()
        all_records = summary.get("cases", {}).get("all_records")
        assert all_records is not None
        assert len(cases_all) == all_records, f"cases?origin=all={len(cases_all)} vs summary.cases.all_records={all_records}"

    def test_dashboard_live_cases_excludes_imported(self, admin_token):
        r_summary = requests.get(f"{BASE}/api/dashboard/summary",
                                 headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)
        assert r_summary.status_code == 200
        s = r_summary.json()
        assert s.get("source") == "database"
        live_cases = s.get("live_cases")
        assert live_cases is not None
        # cross-check
        r_real = requests.get(f"{BASE}/api/cases", params={"origin": "real", "limit": 1000},
                              headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
        open_real = sum(1 for c in r_real.json() if c.get("status") == "open")
        assert live_cases == open_real, f"live_cases={live_cases} vs open_real={open_real}"
        # Ensure imported cases not counted in live
        assert "by_origin" in s.get("cases", {})


# ---- provenance endpoint for 3 cases ----
class TestProvenance:
    def test_provenance_analyst_or_detector(self, admin_token):
        r_real = requests.get(f"{BASE}/api/cases", params={"origin": "real", "limit": 1000},
                              headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
        cases = r_real.json()
        detector = next((c for c in cases if c.get("source") == "dark_spot_detector"), None)
        assert detector, "no detector case found"
        r = requests.get(f"{BASE}/api/cases/{detector['id']}/provenance",
                         headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)
        assert r.status_code == 200
        p = r.json()
        assert p.get("data_mode") == "LIVE DETECTED"
        det = p.get("detection", {})
        assert det.get("badge") == "EXPERIMENTAL"
        assert isinstance(det.get("confidence"), (int, float))
        assert det.get("confidence_source") == "detector"

    def test_provenance_imported(self, admin_token):
        r_imp = requests.get(f"{BASE}/api/cases", params={"origin": "imported", "limit": 5},
                             headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
        c = r_imp.json()[0]
        r = requests.get(f"{BASE}/api/cases/{c['id']}/provenance",
                         headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)
        assert r.status_code == 200
        p = r.json()
        assert p.get("data_mode") == "IMPORTED HISTORICAL"
        assert p.get("data_mode") != "LIVE"
        det = p.get("detection", {})
        assert det.get("confidence") is None
        note = det.get("confidence_note") or p.get("confidence_note") or ""
        assert "N/A" in note or "n/a" in note.lower()

    def test_provenance_target_case(self, admin_token):
        r = requests.get(f"{BASE}/api/cases/{CASE_SPL}/provenance",
                         headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)
        assert r.status_code == 200
        p = r.json()
        analysis = p.get("analysis", {})
        ais = p.get("ais", {})
        assert analysis.get("candidates") == 5
        assert ais.get("observations") == 35


# ---- Candidates factor sum ----
class TestCandidates:
    def test_candidates_factor_sum_equals_score(self, admin_token):
        r = requests.get(f"{BASE}/api/cases/{CASE_SPL}/candidates",
                         headers={"Authorization": f"Bearer {admin_token}"}, timeout=20)
        assert r.status_code == 200
        payload = r.json()
        # could be {candidates:[...]} or list
        cands = payload.get("candidates") if isinstance(payload, dict) else payload
        assert cands and len(cands) >= 1
        for cand in cands:
            factors = cand.get("factors") or {}
            score = cand.get("score")
            assert score is not None
            assert factors, f"no factors on candidate {cand.get('mmsi')}"
            # factors is dict {name: {contribution, weight, score, detail}}
            if isinstance(factors, dict):
                total = sum(f.get("contribution", 0) for f in factors.values())
            else:
                total = sum(f.get("contribution", 0) for f in factors)
            assert abs(total - score) < 0.01, f"cand {cand.get('mmsi')} factor sum {total} vs score {score}"
