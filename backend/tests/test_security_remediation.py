"""Security-remediation tests for iteration 25.

Covers: /api/health production, login (analyst/admin), forgot-password enumeration
parity, jurisdiction regex-escaped search, /api/ais/status (analyst auth), and
/api/system/health data_mode.
"""
import os
import re
import time
import pytest
import requests
from pathlib import Path

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    # read frontend/.env
    for line in Path("/app/frontend/.env").read_text().splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = line.split("=", 1)[1].strip()
            break
BASE_URL = BASE_URL.rstrip("/")

# read test passwords
_env = {}
for line in Path("/app/backend/tests/.env.test").read_text().splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        _env[k.strip()] = v.strip()

ADMIN_EMAIL = "shawpriyanshu950@gmail.com"
ADMIN_PW = _env["TEST_ADMIN_PASSWORD"]
ANALYST_EMAIL = "analyst@sentinelmar.demo"
ANALYST_PW = _env["TEST_ANALYST_PASSWORD"]
SUPERVISOR_EMAIL = "supervisor@sentinelmar.demo"
SUPERVISOR_PW = _env["TEST_SUPERVISOR_PASSWORD"]


@pytest.fixture(scope="module")
def s():
    return requests.Session()


def _login(s, email, pw):
    return s.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": pw}, timeout=20)


# -------- HEALTH --------
def test_health_production(s):
    r = s.get(f"{BASE_URL}/api/health", timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("environment") == "production", d
    assert d.get("demo_mode") is False, d
    assert d.get("database") == "online", d
    ais = d.get("ais", {})
    assert ais.get("key_configured") is True, ais
    assert ais.get("state") in {"CONNECTED", "LIVE", "RECONNECTING", "CONNECTING", "OFFLINE"}, ais


# -------- LOGIN --------
def test_login_admin(s):
    r = _login(s, ADMIN_EMAIL, ADMIN_PW)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "access_token" in d
    assert d["user"]["role"] == "admin"


def test_login_analyst_success(s):
    r = _login(requests.Session(), ANALYST_EMAIL, ANALYST_PW)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "access_token" in d
    assert d["user"]["role"] == "analyst"


def test_login_wrong_password(s):
    # use isolated session, unique-ish wrong pw, only 1 attempt to avoid lockout
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": ANALYST_EMAIL, "password": "definitely-wrong-xyz"}, timeout=15)
    assert r.status_code in (401, 403, 429), r.text


# -------- PASSWORD RESET (enumeration parity) --------
def test_forgot_password_shape_parity():
    r_known = requests.post(f"{BASE_URL}/api/auth/forgot-password", json={"email": ANALYST_EMAIL}, timeout=15)
    r_unknown = requests.post(f"{BASE_URL}/api/auth/forgot-password", json={"email": "nobody-xyz@example.org"}, timeout=15)
    assert r_known.status_code == 200, r_known.text
    assert r_unknown.status_code == 200, r_unknown.text
    dk = r_known.json()
    du = r_unknown.json()
    # Same keys
    assert set(dk.keys()) == set(du.keys()), (dk.keys(), du.keys())
    # Both have message and delivery
    assert "message" in dk and "message" in du
    assert dk.get("delivery") in ("email", "logged")
    assert du.get("delivery") == "email", du  # unknown must claim email (no enumeration)


def test_admin_reset_requests_lists_known(s):
    lr = _login(requests.Session(), ADMIN_EMAIL, ADMIN_PW)
    assert lr.status_code == 200
    token = lr.json()["access_token"]
    rr = requests.get(
        f"{BASE_URL}/api/auth/reset-requests",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    assert rr.status_code == 200, rr.text
    data = rr.json()
    items = data if isinstance(data, list) else data.get("requests", data.get("items", []))
    emails = [i.get("email") for i in items if isinstance(i, dict)]
    assert ANALYST_EMAIL in emails, emails
    # Unknown must NOT be in list
    assert "nobody-xyz@example.org" not in emails, emails


# -------- JURISDICTIONS regex escape --------
def _analyst_headers():
    lr = _login(requests.Session(), ANALYST_EMAIL, ANALYST_PW)
    return {"Authorization": f"Bearer {lr.json()['access_token']}"}


def test_jurisdictions_regex_escaped_metachars():
    h = _analyst_headers()
    t0 = time.time()
    r = requests.get(f"{BASE_URL}/api/jurisdictions", params={"q": "(a+)+$"}, headers=h, timeout=5)
    elapsed = time.time() - t0
    assert r.status_code == 200, r.text
    assert elapsed < 2.0, f"regex too slow: {elapsed}s"
    d = r.json()
    total = d.get("total", len(d.get("items", d if isinstance(d, list) else [])))
    assert total == 0 or isinstance(total, int)


def test_jurisdictions_japan():
    h = _analyst_headers()
    r = requests.get(f"{BASE_URL}/api/jurisdictions", params={"q": "Japan"}, headers=h, timeout=10)
    assert r.status_code == 200, r.text
    body = r.text
    assert "JPN" in body, body[:400]


# -------- AIS status (analyst auth) --------
def test_ais_status_analyst_auth():
    lr = _login(requests.Session(), ANALYST_EMAIL, ANALYST_PW)
    assert lr.status_code == 200, lr.text
    token = lr.json()["access_token"]
    r = requests.get(
        f"{BASE_URL}/api/ais/status",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("state") in {"CONNECTED", "LIVE", "RECONNECTING", "CONNECTING", "OFFLINE"}, d
    body = r.text
    # No 40+ hex tokens (potential API key leak)
    hex_tokens = re.findall(r"[a-f0-9]{40,}", body.lower())
    assert not hex_tokens, f"Possible key leak: {hex_tokens[:2]}"
    assert "APIKey" not in body


# -------- System health --------
def test_system_health_production_mode():
    lr = _login(requests.Session(), ANALYST_EMAIL, ANALYST_PW)
    assert lr.status_code == 200
    token = lr.json()["access_token"]
    r = requests.get(
        f"{BASE_URL}/api/system/health",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("data_mode", {}).get("mode") == "PRODUCTION", d.get("data_mode")


# -------- Regression: dashboard summary --------
def test_dashboard_summary():
    lr = _login(requests.Session(), ANALYST_EMAIL, ANALYST_PW)
    token = lr.json()["access_token"]
    r = requests.get(
        f"{BASE_URL}/api/dashboard/summary",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
