"""Iteration 13 regression: vessel profile query caps + scoped watchlist lookup in case candidates."""
import os
import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") if os.environ.get("REACT_APP_BACKEND_URL") else None
if not BASE:
    # fall back to reading frontend/.env
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE = line.split("=", 1)[1].strip().rstrip("/")
                break

ADMIN_EMAIL = "shawpriyanshu950@gmail.com"
ADMIN_PW = os.environ.get("TEST_ADMIN_PASSWORD", "Admin#2026")


@pytest.fixture(scope="module")
def sess():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PW}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json()["access_token"]
    s.headers["Authorization"] = f"Bearer {tok}"
    return s


def test_vessel_profile_nordic_trader(sess):
    r = sess.get(f"{BASE}/api/vessels/244123456/profile", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mmsi"] == "244123456"
    s = data["ais_summary"]
    assert "fixes" in s and "fixes_analysed" in s
    assert isinstance(s["fixes"], int) and s["fixes"] > 0
    assert s["fixes_analysed"] <= s["fixes"]
    assert s["fixes_analysed"] <= 5000
    assert s["first_seen"] <= s["last_seen"]
    assert isinstance(data["appearances"], list) and len(data["appearances"]) >= 1
    # NORDIC TRADER should appear in SPL-20260610-001
    case_nums = [a["case_number"] for a in data["appearances"]]
    assert "SPL-20260610-001" in case_nums


def test_vessel_profile_unknown_404(sess):
    r = sess.get(f"{BASE}/api/vessels/000000000/profile", timeout=15)
    assert r.status_code == 404


def test_case_candidates_watchlist_scoped(sess):
    cases = sess.get(f"{BASE}/api/cases", timeout=15).json()
    case = next((c for c in cases if c.get("case_number") == "SPL-20260610-001"), None)
    assert case, "seed case SPL-20260610-001 not found"
    r = sess.get(f"{BASE}/api/cases/{case['id']}/candidates", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    cands = data["candidates"]
    assert cands, "no candidates returned"
    atlas = next((c for c in cands if c["mmsi"] == "636998877"), None)
    assert atlas is not None, "ATLAS VOYAGER (636998877) not in candidates"
    assert isinstance(atlas["watchlist"], dict)
    for key in ("reason", "severity", "id"):
        assert key in atlas["watchlist"], f"missing {key} in watchlist"
    non_wl = [c for c in cands if c["mmsi"] != "636998877"]
    # at least one non-watchlisted candidate should have watchlist null
    if non_wl:
        assert any(c["watchlist"] is None for c in non_wl), "expected some candidates to have null watchlist"
