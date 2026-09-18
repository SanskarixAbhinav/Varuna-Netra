"""SentinelMar backend API test suite."""
import os
import time
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
# Fallback to frontend env file
if not BASE:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE = line.split("=", 1)[1].strip()
API = f"{BASE}/api"


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


# ------ Stats + cases seeding ------
def test_stats(s):
    r = s.get(f"{API}/stats")
    assert r.status_code == 200
    d = r.json()
    assert d["cases_total"] >= 3
    assert d["scenes"] >= 2


def test_cases_list_seeded(s):
    r = s.get(f"{API}/cases")
    assert r.status_code == 200
    cases = r.json()
    by_num = {c["case_number"]: c for c in cases}
    assert "SPL-20260610-001" in by_num
    assert "SPL-20260610-002" in by_num
    assert "SPL-20260610-003" in by_num
    c1 = by_num["SPL-20260610-001"]
    # attribution may be analyst_confirmed if a review test ran; automated should stay probable
    assert c1["automated_status"] == "probable"
    assert c1["confidence_band"] == "high"
    assert c1["candidate_count"] >= 5
    assert by_num["SPL-20260610-002"]["automated_status"] == "indeterminate"
    c3 = by_num["SPL-20260610-003"]
    assert c3["automated_status"] == "insufficient_evidence"
    assert c3["degraded"] is True


def _get_case(s, num):
    cases = s.get(f"{API}/cases").json()
    return next(c for c in cases if c["case_number"] == num)


# ------ Candidates for -001 ------
def test_candidates_case_001(s):
    c = _get_case(s, "SPL-20260610-001")
    r = s.get(f"{API}/cases/{c['id']}/candidates")
    assert r.status_code == 200
    d = r.json()
    assert "disclaimer" in d and "input_hash" in d
    cands = d["candidates"]
    assert len(cands) >= 5
    top = cands[0]
    assert top["mmsi"] == "244123456"
    assert top["vessel_name"] == "NORDIC TRADER"
    assert abs(top["score"] - 0.919) < 0.02
    assert top["status"] == "probable"
    for f in ["spatial", "temporal", "continuity", "heading", "drift", "reliability"]:
        assert f in top["factors"]
        for k in ["score", "weight", "contribution", "detail"]:
            assert k in top["factors"][f]
    ocean = next((c for c in cands if c["mmsi"] == "257000123"), None)
    assert ocean is not None
    assert ocean["status"] == "insufficient_evidence"
    notes_text = " ".join(ocean.get("notes") or [])
    assert "no AIS fixes before acquisition time" in notes_text


# ------ Correlate sync + reproducibility ------
def test_correlate_sync_and_reproducible(s):
    c = _get_case(s, "SPL-20260610-001")
    r = s.post(f"{API}/cases/{c['id']}/correlate", json={"sync": True})
    assert r.status_code in (200, 202)
    job = r.json()
    assert job["status"] == "succeeded"
    version1 = job["result"]["version"]
    # rerun
    r2 = s.post(f"{API}/cases/{c['id']}/correlate", json={"sync": True})
    assert r2.status_code in (200, 202)
    ev = s.get(f"{API}/cases/{c['id']}/evidence").json()
    hashes = [rv["input_hash"] for rv in ev["result_versions"]]
    # Last two same-param runs should have identical input_hash
    assert hashes[-1] == hashes[-2]


def test_correlate_async_polling(s):
    c = _get_case(s, "SPL-20260610-001")
    r = s.post(f"{API}/cases/{c['id']}/correlate", json={"sync": False})
    assert r.status_code == 202
    job_id = r.json()["id"]
    for _ in range(20):
        j = s.get(f"{API}/jobs/{job_id}").json()
        if j["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.5)
    assert j["status"] == "succeeded"


def test_correlate_params_override(s):
    c = _get_case(s, "SPL-20260610-001")
    default = s.get(f"{API}/cases/{c['id']}/candidates").json()["candidates"]
    r = s.post(f"{API}/cases/{c['id']}/correlate", json={
        "params": {"corridor_km": 5, "window_hours_before": 6, "window_hours_after": 1, "min_positions": 2},
        "sync": True
    })
    assert r.status_code in (200, 202)
    tight = s.get(f"{API}/cases/{c['id']}/candidates").json()["candidates"]
    assert len(tight) < len(default)
    # restore defaults so review test uses default candidates
    s.post(f"{API}/cases/{c['id']}/correlate", json={"sync": True})


# ------ Review flows (test errors first, confirm last) ------
def test_review_confirm_without_mmsi_400(s):
    c = _get_case(s, "SPL-20260610-001")
    r = s.post(f"{API}/cases/{c['id']}/review", json={
        "decision": "confirm", "reason_codes": ["RC01_TRACK_OVERLAP"], "analyst_id": "t"
    })
    assert r.status_code == 400


def test_review_confirm_bad_mmsi_400(s):
    c = _get_case(s, "SPL-20260610-001")
    r = s.post(f"{API}/cases/{c['id']}/review", json={
        "decision": "confirm", "vessel_mmsi": "999999999",
        "reason_codes": ["RC01_TRACK_OVERLAP"], "analyst_id": "t"
    })
    assert r.status_code == 400


def test_review_unknown_reason_400(s):
    c = _get_case(s, "SPL-20260610-001")
    r = s.post(f"{API}/cases/{c['id']}/review", json={
        "decision": "confirm", "vessel_mmsi": "244123456",
        "reason_codes": ["RC99_UNKNOWN"], "analyst_id": "t"
    })
    assert r.status_code == 400


def test_review_confirm_success(s):
    c = _get_case(s, "SPL-20260610-001")
    prior_versions = len(s.get(f"{API}/cases/{c['id']}/evidence").json()["result_versions"])
    r = s.post(f"{API}/cases/{c['id']}/review", json={
        "decision": "confirm", "vessel_mmsi": "244123456",
        "reason_codes": ["RC01_TRACK_OVERLAP"], "analyst_id": "tester", "notes": "test"
    })
    assert r.status_code in (200, 201)
    updated = _get_case(s, "SPL-20260610-001")
    assert updated["attribution_status"] == "analyst_confirmed"
    assert updated["review_state"] == "confirmed"
    assert updated["confirmed_vessel_mmsi"] == "244123456"
    reviews = s.get(f"{API}/cases/{c['id']}/reviews").json()
    assert len(reviews) >= 1
    # correlate again keeps analyst_confirmed, versions grow
    s.post(f"{API}/cases/{c['id']}/correlate", json={"sync": True})
    after = _get_case(s, "SPL-20260610-001")
    assert after["attribution_status"] == "analyst_confirmed"
    new_versions = len(s.get(f"{API}/cases/{c['id']}/evidence").json()["result_versions"])
    assert new_versions >= prior_versions


# ------ Evidence / GeoJSON ------
def test_evidence_and_geojson(s):
    c1 = _get_case(s, "SPL-20260610-001")
    ev = s.get(f"{API}/cases/{c1['id']}/evidence").json()
    for k in ["case", "source_references", "geometries", "calculations", "result_versions", "reviews", "audit_history", "jobs", "disclaimer"]:
        assert k in ev
    assert ev["calculations"]["algorithm_version"] == "corr-1.0.0"
    gj = s.get(f"{API}/cases/{c1['id']}/geojson").json()
    assert gj["type"] == "FeatureCollection"
    layers = {f["properties"].get("layer") for f in gj["features"]}
    for expected in {"spill", "corridor", "track", "closest_fix", "backprojected_centroid"}:
        assert expected in layers, f"missing layer {expected}"
    # degraded case has no backprojected
    c3 = _get_case(s, "SPL-20260610-003")
    gj3 = s.get(f"{API}/cases/{c3['id']}/geojson").json()
    layers3 = {f["properties"].get("layer") for f in gj3["features"]}
    assert "backprojected_centroid" not in layers3


# ------ AIS ingestion ------
def test_ais_ingest_flags_and_dupes(s):
    batch = {"positions": [
        {"mmsi": "111222333", "vessel_name": "TEST_A", "timestamp": "2026-06-10T04:00:00Z",
         "lat": 53.5, "lon": 3.7, "sog_kn": 12, "cog_deg": 90, "source": "test"},
        {"mmsi": "111222333", "vessel_name": "TEST_A", "timestamp": "2026-06-10T04:00:00Z",
         "lat": 53.5, "lon": 3.7, "sog_kn": 12, "cog_deg": 90, "source": "test"},  # duplicate
        {"mmsi": "111222333", "vessel_name": "TEST_A", "timestamp": "2026-06-10T04:10:00",  # naive
         "lat": 53.6, "lon": 3.8, "sog_kn": 75, "cog_deg": 90, "source": "test"},  # high sog
        {"mmsi": "12345", "vessel_name": "TEST_SHORT", "timestamp": "2026-06-10T04:20:00Z",
         "lat": 53.7, "lon": 3.9, "sog_kn": 10, "cog_deg": 90, "source": "test"},  # short mmsi
    ]}
    r = s.post(f"{API}/ais/positions", json=batch)
    assert r.status_code in (200, 201), r.text
    d = r.json()
    assert d["duplicates"] >= 1
    assert d["flagged"] >= 1
    # repost
    r2 = s.post(f"{API}/ais/positions", json=batch)
    d2 = r2.json()
    assert d2["inserted"] == 0
    assert d2["duplicates"] >= 3


def test_ais_invalid_lat(s):
    r = s.post(f"{API}/ais/positions", json={"positions": [
        {"mmsi": "111222444", "timestamp": "2026-06-10T04:00:00Z",
         "lat": 95, "lon": 3.7, "source": "t"}
    ]})
    assert r.status_code == 422


def test_ais_vessels_and_positions(s):
    r = s.get(f"{API}/ais/vessels")
    assert r.status_code == 200 and isinstance(r.json()["indexed"], list) and isinstance(r.json()["vessels"], list)
    r = s.get(f"{API}/ais/positions", params={"mmsi": "244123456"})
    assert r.status_code == 200


# ------ Spill observations ------
def test_spill_valid_and_invalid(s):
    scenes = s.get(f"{API}/scenes").json()
    scene_id = scenes[0]["id"]
    good_poly = {"type": "Polygon", "coordinates": [[[3.71, 53.55], [3.72, 53.55], [3.72, 53.56], [3.71, 53.56], [3.71, 53.55]]]}
    r = s.post(f"{API}/spill-observations", json={
        "scene_id": scene_id, "geometry": good_poly, "acquisition_time": "2026-06-10T05:30:00Z",
        "source": "test", "detection_confidence": 0.8, "quality_flags": [], "processing_version": "test"
    })
    assert r.status_code == 201, r.text
    d = r.json()
    assert "spill_observation" in d and "case" in d
    assert d["case"]["case_number"].startswith("SPL-")
    assert d["case"]["attribution_status"] == "indeterminate"
    assert d["spill_observation"]["estimated_area_km2"] > 0

    # invalid geometry - self-intersecting bowtie
    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    r = s.post(f"{API}/spill-observations", json={
        "scene_id": scene_id, "geometry": bowtie, "acquisition_time": "2026-06-10T05:30:00Z",
        "source": "test", "detection_confidence": 0.5, "quality_flags": [], "processing_version": "t"
    })
    assert r.status_code == 400, r.text

    # Point instead of polygon
    r = s.post(f"{API}/spill-observations", json={
        "scene_id": scene_id, "geometry": {"type": "Point", "coordinates": [3.7, 53.5]},
        "acquisition_time": "2026-06-10T05:30:00Z", "source": "t", "detection_confidence": 0.5,
        "quality_flags": [], "processing_version": "t"
    })
    assert r.status_code == 400, r.text

    # Unknown quality flag
    r = s.post(f"{API}/spill-observations", json={
        "scene_id": scene_id, "geometry": good_poly, "acquisition_time": "2026-06-10T05:30:00Z",
        "source": "t", "detection_confidence": 0.5, "quality_flags": ["nonsense_flag_xyz"], "processing_version": "t"
    })
    assert r.status_code == 400, r.text

    # Unknown scene_id
    r = s.post(f"{API}/spill-observations", json={
        "scene_id": "00000000-0000-0000-0000-000000000000", "geometry": good_poly,
        "acquisition_time": "2026-06-10T05:30:00Z", "source": "t", "detection_confidence": 0.5,
        "quality_flags": [], "processing_version": "t"
    })
    assert r.status_code == 400, r.text


# ------ Scenes + detect ------
def test_scene_create_duplicate_and_detect(s):
    payload = {
        "provider": "sentinel-1", "provider_scene_id": f"TEST_SCENE_{int(time.time())}",
        "sensor_mode": "IW", "polarization": "VV",
        "acquisition_time": "2026-06-12T00:00:00Z",
        "footprint": {"type": "Polygon", "coordinates": [[[3.0, 53.0], [4.0, 53.0], [4.0, 54.0], [3.0, 54.0], [3.0, 53.0]]]},
        "storage_ref": "s3://test/x.tiff", "metadata": {}
    }
    r = s.post(f"{API}/scenes", json=payload)
    assert r.status_code == 201, r.text
    r2 = s.post(f"{API}/scenes", json=payload)
    assert r2.status_code == 400
    # bad footprint
    bad = dict(payload); bad["provider_scene_id"] = f"TEST_BAD_{int(time.time())}"
    bad["footprint"] = {"type": "Point", "coordinates": [0, 0]}
    r3 = s.post(f"{API}/scenes", json=bad)
    assert r3.status_code == 400

    # detect on scene2 (S1B)
    scenes = s.get(f"{API}/scenes").json()
    s1b = next(sc for sc in scenes if sc["provider_scene_id"] == "S1B_IW_GRDH_1SDV_20260611T174500_DEMO")
    r = s.post(f"{API}/scenes/{s1b['id']}/detect")
    assert r.status_code in (200, 201, 202)
    job = r.json()
    job_id = job.get("id") or job.get("job_id")
    if job_id:
        for _ in range(30):
            j = s.get(f"{API}/jobs/{job_id}").json()
            if j["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.5)


# ------ Alerts / jobs / config / audit ------
def test_alerts_ack(s):
    alerts = s.get(f"{API}/alerts").json()
    assert isinstance(alerts, list) and len(alerts) >= 1
    unack = next((a for a in alerts if not a.get("acknowledged")), None)
    if unack:
        r = s.post(f"{API}/alerts/{unack['id']}/ack")
        assert r.status_code == 200
        after = next(a for a in s.get(f"{API}/alerts").json() if a["id"] == unack["id"])
        assert after["acknowledged"] is True


def test_jobs_list_and_404(s):
    r = s.get(f"{API}/jobs")
    assert r.status_code == 200
    r = s.get(f"{API}/jobs/nonexistent-id-xxx")
    assert r.status_code == 404


def test_config_defaults(s):
    r = s.get(f"{API}/config/defaults")
    assert r.status_code == 200
    d = r.json()
    assert d.get("algorithm_version") == "corr-1.0.0"
    assert "reason_codes" in d
    assert "correlation_params" in d


def test_audit_and_case_404(s):
    r = s.get(f"{API}/audit")
    assert r.status_code == 200
    r = s.get(f"{API}/cases/nonexistent-id-xxx")
    assert r.status_code == 404
