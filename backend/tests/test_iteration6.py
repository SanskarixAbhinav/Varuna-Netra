"""Iteration 6 tests: satellite (Planetary Computer STAC) + AIS live (aisstream.io)."""
import os
import time

import pytest
import requests

def _load_env_url():
    u = os.environ.get("REACT_APP_BACKEND_URL")
    if u:
        return u
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


BASE_URL = _load_env_url().rstrip("/")
API = f"{BASE_URL}/api"

ADMIN = ("shawpriyanshu950@gmail.com", os.environ["TEST_ADMIN_PASSWORD"])
SUPERVISOR = ("supervisor@sentinelmar.demo", os.environ["TEST_SUPERVISOR_PASSWORD"])
ANALYST = ("analyst@sentinelmar.demo", os.environ["TEST_ANALYST_PASSWORD"])


def _login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def admin_h():
    return {"Authorization": f"Bearer {_login(*ADMIN)}"}


@pytest.fixture(scope="module")
def analyst_h():
    return {"Authorization": f"Bearer {_login(*ANALYST)}"}


@pytest.fixture(scope="module")
def supervisor_h():
    return {"Authorization": f"Bearer {_login(*SUPERVISOR)}"}


# ---------------- satellite/collections ----------------
def test_collections(analyst_h):
    r = requests.get(f"{API}/satellite/collections", headers=analyst_h, timeout=30)
    assert r.status_code == 200
    d = r.json()
    ids = [c["id"] for c in d["collections"]]
    assert "sentinel-1-grd" in ids and "sentinel-2-l2a" in ids
    assert d.get("gibs_template", "").startswith("https://gibs-")
    assert isinstance(d.get("basemaps"), list) and len(d["basemaps"]) >= 1


# ---------------- satellite/search ----------------
def test_search_bad_collection(analyst_h):
    r = requests.post(f"{API}/satellite/search", headers=analyst_h,
                      json={"bbox": [0, 0, 1, 1], "start": "2026-05-01", "end": "2026-06-01", "collection": "fake"}, timeout=30)
    assert r.status_code == 400


def test_search_bad_bbox(analyst_h):
    # inverted
    r = requests.post(f"{API}/satellite/search", headers=analyst_h,
                      json={"bbox": [5, 5, 0, 0], "start": "2026-05-01", "end": "2026-06-01", "collection": "sentinel-1-grd"}, timeout=30)
    assert r.status_code == 400
    # out of range
    r = requests.post(f"{API}/satellite/search", headers=analyst_h,
                      json={"bbox": [-200, 0, 200, 10], "start": "2026-05-01", "end": "2026-06-01", "collection": "sentinel-1-grd"}, timeout=30)
    assert r.status_code == 400


def test_search_area_too_large(analyst_h):
    r = requests.post(f"{API}/satellite/search", headers=analyst_h,
                      json={"bbox": [-90, -40, 90, 40], "start": "2026-05-01", "end": "2026-06-01", "collection": "sentinel-1-grd"}, timeout=30)
    assert r.status_code == 400


@pytest.fixture(scope="module")
def search_result(analyst_h):
    """Real STAC search — Malacca 2026-05-15..2026-06-07."""
    r = requests.post(f"{API}/satellite/search", headers=analyst_h,
                      json={"bbox": [98, -1, 105, 6], "start": "2026-05-15", "end": "2026-06-07",
                            "collection": "sentinel-1-grd", "limit": 5}, timeout=90)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] >= 1
    return d


def test_search_s1_shape(search_result):
    sc = search_result["scenes"][0]
    for k in ("stac_id", "datetime", "platform", "footprint"):
        assert k in sc
    assert "registered_scene_id" in sc


def test_search_s2_max_cloud(analyst_h):
    r = requests.post(f"{API}/satellite/search", headers=analyst_h,
                      json={"bbox": [98, -1, 105, 6], "start": "2026-05-15", "end": "2026-06-07",
                            "collection": "sentinel-2-l2a", "limit": 3, "max_cloud": 30}, timeout=90)
    assert r.status_code == 200, r.text
    d = r.json()
    assert isinstance(d.get("scenes"), list)
    for sc in d["scenes"]:
        cc = sc.get("cloud_cover")
        if cc is not None:
            assert cc < 30


# ---------------- satellite/preview ----------------
def test_preview_unknown_collection(analyst_h):
    r = requests.get(f"{API}/satellite/preview", headers=analyst_h,
                     params={"collection": "fake", "stac_id": "x"}, timeout=30)
    assert r.status_code == 400


def test_preview_png(analyst_h, search_result):
    sid = search_result["scenes"][0]["stac_id"]
    r = requests.get(f"{API}/satellite/preview", headers=analyst_h,
                     params={"collection": "sentinel-1-grd", "stac_id": sid}, timeout=180)
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get("content-type", "").startswith("image/")
    assert len(r.content) > 1000


# ---------------- satellite/register ----------------
@pytest.fixture(scope="module")
def registered(analyst_h, search_result):
    # find a scene not yet registered
    target = None
    for sc in search_result["scenes"]:
        if not sc.get("registered_scene_id"):
            target = sc
            break
    if target is None:
        target = search_result["scenes"][0]
    r = requests.post(f"{API}/satellite/register", headers=analyst_h,
                      json={"collection": "sentinel-1-grd", "stac_id": target["stac_id"], "detect": False}, timeout=90)
    assert r.status_code == 201, r.text
    d = r.json()
    return d, target


def test_register_scene_shape(registered):
    d, target = registered
    assert "scene" in d
    sc = d["scene"]
    assert sc["provider"] == "sentinel-1"
    assert sc["provider_scene_id"] == target["stac_id"]
    assert sc["storage_ref"].startswith("https://planetarycomputer")
    assert "preview_href" in (sc.get("metadata") or {})


def test_register_appears_in_scenes(analyst_h, registered):
    d, _ = registered
    sid = d["scene"]["id"]
    r = requests.get(f"{API}/scenes", headers=analyst_h, timeout=30)
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()]
    assert sid in ids


def test_register_idempotent(analyst_h, registered):
    _, target = registered
    r = requests.post(f"{API}/satellite/register", headers=analyst_h,
                      json={"collection": "sentinel-1-grd", "stac_id": target["stac_id"]}, timeout=60)
    assert r.status_code == 201
    d = r.json()
    assert d["already_registered"] is True


def test_register_with_detect(analyst_h, search_result):
    # pick a fresh scene for detect
    target = None
    for sc in search_result["scenes"][::-1]:
        # ensure not registered yet
        rr = requests.get(f"{API}/scenes", headers=analyst_h, timeout=30).json()
        registered_ids = {s["provider_scene_id"] for s in rr}
        if sc["stac_id"] not in registered_ids:
            target = sc
            break
    if target is None:
        pytest.skip("no unregistered scene available for detect")
    r = requests.post(f"{API}/satellite/register", headers=analyst_h,
                      json={"collection": "sentinel-1-grd", "stac_id": target["stac_id"], "detect": True}, timeout=120)
    assert r.status_code == 201, r.text
    d = r.json()
    assert "case" in d and "spill_observation" in d and "detector_note" in d
    assert "mock" in d["detector_note"].lower() or "placeholder" in d["detector_note"].lower()


# ---------------- ais/live (canonical: env-only key, settings.ais_coverage) ----------------
def test_live_status_canonical(analyst_h):
    r = requests.get(f"{API}/ais/live/status", headers=analyst_h, timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "AISStream" and d["mode"] == "live"
    assert d["state"] in ("NOT_CONFIGURED", "CONNECTING", "CONNECTED", "LIVE", "RECONNECTING", "OFFLINE")
    assert "api_key" not in d and "key" not in d
    assert isinstance(d["coverage_bbox"], list) and all(len(b) == 4 for b in d["coverage_bbox"])
    assert d["worker_running"] is True


def test_legacy_live_settings_endpoint_removed(admin_h):
    r = requests.put(f"{API}/ais/live/settings", headers=admin_h, json={"api_key": "x", "bboxes": [[[10, -5], [20, 5]]]}, timeout=15)
    assert r.status_code in (404, 405)


# ---------------- Regression: seeded roles/data still healthy ----------------
def test_dashboard_loads(analyst_h):
    r = requests.get(f"{API}/cases", headers=analyst_h, timeout=15)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_zone_rules_still_accessible(analyst_h):
    r = requests.get(f"{API}/zone-rules", headers=analyst_h, timeout=15)
    assert r.status_code == 200
