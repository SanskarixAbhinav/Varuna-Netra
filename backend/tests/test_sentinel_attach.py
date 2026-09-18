"""Adaptive Sentinel-1 scene search + attachment — real Planetary Computer STAC output only (no fabricated scenes)."""
import os
from datetime import datetime, timezone

import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE or "TEST_ANALYST_PASSWORD" not in os.environ, reason="live API env not available")


@pytest.fixture(scope="module")
def h():
    tok = requests.post(f"{BASE}/api/auth/login", json={"email": "analyst@sentinelmar.demo", "password": os.environ["TEST_ANALYST_PASSWORD"]}, timeout=15).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _nearest(h, lon, lat, target, **kw):
    r = requests.get(f"{BASE}/api/sentinel/nearest", params={"lon": lon, "lat": lat, "target": target, **kw}, headers=h, timeout=150)
    return r


@pytest.mark.parametrize("lon,lat", [(72, 19), (52, 26), (3, 54), (-90, 27), (140, 35), (101, 3)], ids=["arabian-sea", "persian-gulf", "north-sea", "gulf-of-mexico", "japan", "malacca"])
def test_nearest_worldwide_staged(h, lon, lat):
    d = _nearest(h, lon, lat, "2026-09-09T12:00:00Z").json()
    assert d["state"] in ("FOUND_36H", "NEAREST_FOUND_EXTENDED", "NO_COVERAGE_7D", "STAC_FAILED")
    stages = [s["window_hours"] for s in d["stages_tried"]]
    assert stages == [36, 72, 120, 168][: len(stages)]  # staged: 36 → 72 → 120 → 168, stops at first hit
    if d["found"]:
        s = d["scene"]
        assert s["scene_id"].startswith("S1") and s["time_difference_hours"] <= d["search_window_hours"]
        assert (d["state"] == "FOUND_36H") == (d["search_window_hours"] == 36)
        assert s["sar_available"] is True and s["overlap_percent"] is not None


def test_invalid_and_reversed_coordinates(h):
    assert _nearest(h, 200, 5, "2026-09-09T12:00:00Z").status_code == 400
    assert _nearest(h, 72, 95, "2026-09-09T12:00:00Z").status_code == 400  # lat/lon swapped past range
    assert _nearest(h, 72, 19, "not-a-date").status_code == 400


def test_no_coverage_small_window(h):
    d = _nearest(h, -40, 21, "2026-09-09T12:00:00Z", max_hours=1).json()  # 1 h window: revisit gap is essentially certain
    if not d["found"]:
        assert d["state"] == "NO_COVERAGE_7D" and "revisit gap" in d["reason"]


def test_case_candidates_attach_persist_and_manual(h):
    cases = requests.get(f"{BASE}/api/cases?limit=1000", headers=h, timeout=30).json()
    case = next((c for c in cases if c.get("scene_attachment")), None) or cases[0]
    cid = case["id"]
    cands = requests.get(f"{BASE}/api/cases/{cid}/scene-candidates", headers=h, timeout=200).json()
    assert cands["primary"].startswith("Sentinel-1") and "supplementary" in cands and cands["stages_tried"]
    if not cands["found"]:
        assert cands["state"] in ("NO_COVERAGE_7D", "STAC_FAILED")
        st = requests.get(f"{BASE}/api/cases/{cid}", headers=h, timeout=30).json()["scene_status"]
        assert st["sar_confirmation"] in ("PENDING", "READY")
        return
    best = cands["candidates"][0]
    for k in ("scene_id", "acquisition_time", "time_difference_hours", "overlap_percent", "sar_available", "polarization", "platform", "score"):
        assert k in best
    # manual selection of the top candidate (may register from STAC)
    r = requests.post(f"{BASE}/api/cases/{cid}/attach-scene", json={"scene_id": best["scene_id"]}, headers=h, timeout=200)
    assert r.status_code == 200, r.text[:300]
    att = r.json()["attachment"]
    assert att["provider_scene_id"] == best["scene_id"] and att["auto"] is False and att["time_difference_hours"] == best["time_difference_hours"]
    for k in ("case_id", "spill_id", "scene_id", "acquisition_time", "scene_bbox", "scene_geometry", "analysis_asset_key", "provider"):
        assert k in att
    # persistence: a fresh GET (what the browser does after refresh) shows the same scene + metrics
    fresh = requests.get(f"{BASE}/api/cases/{cid}", headers=h, timeout=30).json()
    assert fresh["scene_id"] == att["scene_id"] and fresh["scene_status"]["state"] in ("SAR_READY", "QUICKLOOK_GENERATING")
    assert fresh["scene_status"]["time_difference_hours"] == att["time_difference_hours"]
    assert datetime.fromisoformat(fresh["scene_status"]["acquisition_time"].replace("Z", "+00:00")).tzinfo is not None
    # auto-selection on the same case picks the best-ranked scene deterministically
    r2 = requests.post(f"{BASE}/api/cases/{cid}/attach-scene", json={}, headers=h, timeout=200)
    assert r2.status_code == 200 and r2.json()["attachment"]["auto"] is True
    assert r2.json()["attachment"]["provider_scene_id"] == best["scene_id"]
