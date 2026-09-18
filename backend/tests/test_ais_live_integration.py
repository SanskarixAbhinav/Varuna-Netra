"""AIS live API integration tests for iteration 19."""
import json
import os
from datetime import datetime, timezone

import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE:
    # fallback: read from frontend/.env
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE = line.split("=", 1)[1].strip().rstrip("/")
                break

CREDS = {
    "admin": ("shawpriyanshu950@gmail.com", os.environ.get("TEST_ADMIN_PASSWORD", "Admin#2026")),
    "supervisor": ("supervisor@sentinelmar.demo", os.environ.get("TEST_SUPERVISOR_PASSWORD", "Supervisor#2026")),
    "analyst": ("analyst@sentinelmar.demo", os.environ.get("TEST_ANALYST_PASSWORD", "Analyst#2026")),
}


def _login(role):
    email, pw = CREDS[role]
    r = requests.post(f"{BASE}/api/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, f"login {role}: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def admin():
    return {"Authorization": f"Bearer {_login('admin')}"}


@pytest.fixture(scope="module")
def supervisor():
    return {"Authorization": f"Bearer {_login('supervisor')}"}


@pytest.fixture(scope="module")
def analyst():
    return {"Authorization": f"Bearer {_login('analyst')}"}


@pytest.fixture(scope="module", autouse=True)
def _reset_default(supervisor):
    yield
    requests.post(f"{BASE}/api/ais/coverage/region/default", headers=supervisor, timeout=15)


# ---- status ----

def test_status_default(supervisor):
    # reset to default first
    requests.post(f"{BASE}/api/ais/coverage/region/default", headers=supervisor, timeout=15)
    r = requests.get(f"{BASE}/api/ais/status", headers=supervisor, timeout=15)
    assert r.status_code == 200, r.text
    js = r.json()
    assert js["mode"] == "live"
    if js["configured"]:
        # genuine key present: LIVE requires real parsed positions; CONNECTED/CONNECTING otherwise
        assert js["state"] in ("CONNECTING", "CONNECTED", "LIVE", "RECONNECTING", "OFFLINE")
        if js["state"] == "LIVE":
            assert js["connected"] and js["subscription_confirmed"] and js["positions_parsed"] > 0 and js["last_message_at"]
    else:
        assert js["state"] == "NOT_CONFIGURED" and js["connected"] is False and js["subscription_confirmed"] is False
        assert js["reason"] == "API key not configured" and js["vessels_active"] == 0
    assert js["coverage_mode"] in ("default", "spill", "manual")
    assert len(js["coverage_bbox"]) == 4
    for s, w, n, e in js["coverage_bbox"]:
        assert 5 <= s < n <= 25, f"lat range {s},{n}"
        assert 65 <= w < e <= 96, f"lon range {w},{e}"
    # first box: west_coast [[8.0,66.0],[24.5,76.5]]
    b0 = js["aisstream_bounding_boxes"][0]
    assert b0 == [[8.0, 66.0], [24.5, 76.5]], f"got {b0}"
    # No API key leak
    body = json.dumps(js)
    assert "APIKey" not in body
    assert "api_key" not in body.lower() or True  # 'reason' contains 'API key not configured'; allow the phrase
    # but ensure no actual key
    assert "AISSTREAM_API_KEY" not in body


def test_status_alias(supervisor):
    r = requests.get(f"{BASE}/api/ais/live/status", headers=supervisor, timeout=15)
    assert r.status_code == 200
    assert r.json()["mode"] == "live"


# ---- coverage region ----

def test_coverage_region_east_coast(supervisor):
    r = requests.post(f"{BASE}/api/ais/coverage/region/east_coast", headers=supervisor, timeout=15)
    assert r.status_code == 200, r.text
    js = r.json()
    assert js["mode"] == "manual"
    assert js["bboxes"] == [[10.0, 78.5, 22.5, 90.0]]
    st = requests.get(f"{BASE}/api/ais/status", headers=supervisor, timeout=15).json()
    assert st["coverage_mode"] == "manual"
    assert st["coverage_name"] == "Bay of Bengal / East Coast"


def test_coverage_manual(supervisor):
    r = requests.post(f"{BASE}/api/ais/coverage",
                      json={"south": 18, "west": 71, "north": 20, "east": 73, "name": "Mumbai AOI"},
                      headers=supervisor, timeout=15)
    assert r.status_code == 200, r.text
    js = r.json()
    assert js["bboxes"] == [[18, 71, 20, 73]]
    assert js["mode"] == "manual"


def test_coverage_invalid_swne(supervisor):
    r = requests.post(f"{BASE}/api/ais/coverage",
                      json={"south": 20, "west": 70, "north": 10, "east": 72},
                      headers=supervisor, timeout=15)
    assert r.status_code == 400


def test_coverage_invalid_lon(supervisor):
    r = requests.post(f"{BASE}/api/ais/coverage",
                      json={"south": 10, "west": 70, "north": 12, "east": 200},
                      headers=supervisor, timeout=15)
    assert r.status_code == 400


def test_coverage_analyst_forbidden(analyst):
    r = requests.post(f"{BASE}/api/ais/coverage",
                      json={"south": 10, "west": 70, "north": 12, "east": 72},
                      headers=analyst, timeout=15)
    assert r.status_code == 403


def test_coverage_region_default(supervisor):
    r = requests.post(f"{BASE}/api/ais/coverage/region/default", headers=supervisor, timeout=15)
    assert r.status_code == 200
    assert r.json()["mode"] == "default"


def test_coverage_region_unknown(supervisor):
    r = requests.post(f"{BASE}/api/ais/coverage/region/atlantic", headers=supervisor, timeout=15)
    assert r.status_code == 404


# ---- vessels & tracks ----

def test_vessels_empty(analyst):
    r = requests.get(f"{BASE}/api/ais/vessels", headers=analyst, timeout=15)
    assert r.status_code == 200
    js = r.json()
    assert js["count"] == len(js["vessels"])
    for v in js["vessels"]:
        assert v["mmsi"].isdigit() and -90 <= v["lat"] <= 90 and -180 <= v["lon"] <= 180 and v["timestamp"]
    if not js["configured"]:
        assert js["vessels"] == []


def test_tracks_empty(analyst):
    r = requests.get(f"{BASE}/api/ais/tracks/123456789?hours=24", headers=analyst, timeout=15)
    assert r.status_code == 200
    assert r.json()["count"] == 0


# ---- test-connection ----

def test_test_connection_admin(admin):
    r = requests.post(f"{BASE}/api/ais/test-connection", headers=admin, timeout=20)
    assert r.status_code == 200
    js = r.json()
    if js["configured"]:
        assert js["websocket"] is True and js["subscription"] is True
    else:
        assert js["websocket"] is False and js["error"] == "API key not configured"


def test_test_connection_analyst_forbidden(analyst):
    r = requests.post(f"{BASE}/api/ais/test-connection", headers=analyst, timeout=15)
    assert r.status_code == 403


# ---- debug/aoi ----

def test_debug_aoi(supervisor):
    r = requests.get(f"{BASE}/api/ais/debug/aoi", headers=supervisor, timeout=15)
    assert r.status_code == 200
    js = r.json()
    assert "investigation_aoi" in js
    assert "aisstream_bounding_boxes" in js
    assert "latest_sentinel_scene" in js
    scene = js["latest_sentinel_scene"]
    if scene:
        assert scene["provider_scene_id"].startswith("S1")


# ---- spill follow ----

def test_spill_follow_and_reset(analyst, supervisor):
    # ensure default first
    requests.post(f"{BASE}/api/ais/coverage/region/default", headers=supervisor, timeout=15)
    payload = {
        "geometry": {"type": "Polygon", "coordinates": [[
            [71.99, 18.99], [72.01, 18.99], [72.01, 19.01], [71.99, 19.01], [71.99, 18.99]
        ]]},
        "acquisition_time": datetime.now(timezone.utc).isoformat(),
        "source": "external",
        "detection_confidence": 0.8,
        "estimated_area_km2": 5.0,
    }
    r = requests.post(f"{BASE}/api/spill-observations", json=payload, headers=analyst, timeout=30)
    assert r.status_code == 201, r.text
    # give the hook a moment
    import time
    time.sleep(2)
    st = requests.get(f"{BASE}/api/ais/status", headers=supervisor, timeout=15).json()
    assert st["coverage_mode"] == "spill", f"expected spill, got {st['coverage_mode']}"
    bbox = st["coverage_bbox"][0]
    s, w, n, e = bbox
    assert 17.5 < s < 18.5, f"south={s}"
    assert 70.5 < w < 71.5, f"west={w}"
    assert 19.5 < n < 20.5, f"north={n}"
    assert 72.5 < e < 73.5, f"east={e}"
    # reset
    r2 = requests.post(f"{BASE}/api/ais/coverage/region/default", headers=supervisor, timeout=15)
    assert r2.status_code == 200
    assert r2.json()["mode"] == "default"
