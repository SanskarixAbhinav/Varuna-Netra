"""Iteration 10 tests — Indian maritime territories (Territorial Sea / Contiguous / EEZ).
Covers: /api/jurisdictions listing, geojson, marine-regions import RBAC + validation,
spill zone resolution across all 3 bands + open ocean, and candidate.zone tags.
"""
import os
import time
from datetime import datetime, timezone, timedelta

import pytest
import requests

def _read_base_url():
    v = os.environ.get("REACT_APP_BACKEND_URL")
    if v:
        return v.rstrip("/")
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip().rstrip("/")
    except OSError:
        pass
    raise RuntimeError("REACT_APP_BACKEND_URL not configured")


BASE_URL = _read_base_url()
API = f"{BASE_URL}/api"

ADMIN = {"email": "shawpriyanshu950@gmail.com", "password": os.environ["TEST_ADMIN_PASSWORD"]}
ANALYST = {"email": "analyst@sentinelmar.demo", "password": os.environ["TEST_ANALYST_PASSWORD"]}
SUPERVISOR = {"email": "supervisor@sentinelmar.demo", "password": os.environ["TEST_SUPERVISOR_PASSWORD"]}


def _login(session, creds):
    r = session.post(f"{API}/auth/login", json=creds, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json()["access_token"]
    session.headers.update({"Authorization": f"Bearer {tok}"})
    return tok


@pytest.fixture(scope="module")
def admin_s():
    s = requests.Session(); _login(s, ADMIN); return s


@pytest.fixture(scope="module")
def analyst_s():
    s = requests.Session(); _login(s, ANALYST); return s


@pytest.fixture(scope="module")
def supervisor_s():
    s = requests.Session(); _login(s, SUPERVISOR); return s


# ---------- Jurisdictions listing ----------
class TestJurisdictions:
    def test_list_includes_ind_zones(self, analyst_s):
        r = analyst_s.get(f"{API}/jurisdictions", timeout=30)
        assert r.status_code == 200
        by_code = {z["code"]: z for z in r.json()}
        for code, ztype in [("IND-TS", "territorial"), ("IND-CZ", "contiguous"), ("IND-EEZ", "eez")]:
            assert code in by_code, f"{code} missing from /api/jurisdictions"
            z = by_code[code]
            assert z["zone_type"] == ztype
            assert z.get("official") is True
            assert z.get("zone_label"), f"zone_label missing for {code}"

    def test_geojson_has_zone_type(self, analyst_s):
        r = analyst_s.get(f"{API}/jurisdictions/geojson", timeout=30)
        assert r.status_code == 200
        fc = r.json()
        ztypes = {f["properties"].get("zone_type") for f in fc["features"]}
        assert "territorial" in ztypes
        assert "contiguous" in ztypes
        assert "eez" in ztypes


# ---------- Marine Regions import RBAC + validation ----------
class TestImportRBAC:
    def test_analyst_forbidden(self, analyst_s):
        r = analyst_s.post(f"{API}/jurisdictions/import/marine-regions",
                           json={"iso3": ["IND"], "layers": ["eez_12nm"], "replace_demo": False}, timeout=30)
        assert r.status_code == 403

    def test_invalid_layer_400(self, admin_s):
        r = admin_s.post(f"{API}/jurisdictions/import/marine-regions",
                         json={"iso3": ["IND"], "layers": ["bogus"], "replace_demo": False}, timeout=30)
        assert r.status_code == 400

    def test_admin_import_202(self, admin_s):
        r = admin_s.post(f"{API}/jurisdictions/import/marine-regions",
                         json={"iso3": ["IND"], "layers": ["eez_12nm"], "replace_demo": False}, timeout=30)
        assert r.status_code == 202
        job = r.json()
        assert "id" in job
        # Do NOT wait for completion — external network. Just verify job accepted.


# ---------- Spill zone resolution across bands ----------
def _small_polygon(lon, lat, d=0.02):
    return {
        "type": "Polygon",
        "coordinates": [[[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]],
    }


def _create_spill(session, lon, lat, name):
    body = {
        "source": "sar",
        "acquisition_time": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "geometry": _small_polygon(lon, lat),
        "detection_confidence": 0.85,
        "sensor_metadata": {"sensor": "TEST_iter10", "note": name},
    }
    r = session.post(f"{API}/spill-observations", json=body, timeout=60)
    assert r.status_code in (200, 201), f"spill create failed for {name}: {r.status_code} {r.text}"
    return r.json()


class TestSpillZoneResolution:
    def test_territorial_ts(self, analyst_s):
        r = _create_spill(analyst_s, 72.62, 18.95, "TS_off_mumbai_8nm")
        case = r.get("case") or r
        pj = case.get("primary_jurisdiction")
        assert pj, f"primary_jurisdiction missing: {case}"
        assert pj["code"] == "IND-TS", f"expected IND-TS, got {pj['code']}"
        assert pj.get("zone_label") == "Territorial Sea (12 NM)"
        codes = {z["code"] for z in case.get("jurisdictions", [])}
        assert "IND-EEZ" in codes, f"IND-EEZ should also be in jurisdictions: {codes}"

    def test_contiguous_cz(self, analyst_s):
        r = _create_spill(analyst_s, 72.45, 18.95, "CZ_off_mumbai")
        case = r.get("case") or r
        pj = case.get("primary_jurisdiction")
        assert pj and pj["code"] == "IND-CZ", f"expected IND-CZ, got {pj}"
        assert pj.get("zone_label") == "Contiguous Zone (24 NM)"

    def test_eez(self, analyst_s):
        r = _create_spill(analyst_s, 71.0, 18.9, "EEZ_arabian_sea")
        case = r.get("case") or r
        pj = case.get("primary_jurisdiction")
        assert pj and pj["code"] == "IND-EEZ", f"expected IND-EEZ, got {pj}"

    def test_open_ocean_null(self, analyst_s):
        r = _create_spill(analyst_s, 60.0, 10.0, "open_ocean")
        case = r.get("case") or r
        pj = case.get("primary_jurisdiction")
        assert pj is None, f"expected null primary in open ocean, got {pj}"


# ---------- Candidate zone tags ----------
class TestCandidateZones:
    def test_correlate_spl001_has_zone(self, analyst_s):
        # find SPL-20260610-001
        r = analyst_s.get(f"{API}/cases?limit=200", timeout=30)
        assert r.status_code == 200
        payload = r.json()
        cases = payload if isinstance(payload, list) else payload.get("cases") or payload.get("items") or []
        target = next((c for c in cases if c.get("case_number") == "SPL-20260610-001"), None)
        assert target, "SPL-20260610-001 not found"
        cid = target["id"]

        # correlate synchronously
        r = analyst_s.post(f"{API}/cases/{cid}/correlate", json={"sync": True}, timeout=180)
        assert r.status_code in (200, 201, 202), f"correlate failed: {r.status_code} {r.text}"

        # allow a moment for candidates persistence
        time.sleep(2)
        r = analyst_s.get(f"{API}/cases/{cid}/candidates", timeout=30)
        assert r.status_code == 200
        cands = r.json().get("candidates") or []
        assert len(cands) > 0, "no candidates returned"
        # each candidate should have a zone (or zone None + zones list)
        with_zone = 0
        for c in cands:
            assert "zones" in c, f"candidate missing zones field: {c.get('mmsi')}"
            if c.get("zone"):
                with_zone += 1
                assert "code" in c["zone"]
                assert "zone_label" in c["zone"]
        assert with_zone >= 1, "expected at least one candidate with zone tag (NLD-EEZ)"
