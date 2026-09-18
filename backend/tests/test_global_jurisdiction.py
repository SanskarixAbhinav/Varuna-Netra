"""Worldwide jurisdiction + AOI pipeline — real API output only (Marine Regions zones in Mongo, real Planetary Computer STAC, real AIS coverage settings)."""
import os
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE or "TEST_ANALYST_PASSWORD" not in os.environ, reason="live API env not available")


@pytest.fixture(scope="module")
def h():
    tok = requests.post(f"{BASE}/api/auth/login", json={"email": "supervisor@sentinelmar.demo", "password": os.environ["TEST_SUPERVISOR_PASSWORD"]}, timeout=15).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


REGIONS = [  # name, search query, expected primary country code, probe point (lon, lat)
    ("India / Mumbai / Arabian Sea", "India", "IND", (72.0, 19.0)),
    ("Belgium / North Sea", "Belgi", "BEL", (2.8, 51.5)),
    ("United Kingdom / North Sea", "British", "GBR", (1.5, 54.0)),
    ("United States / Gulf of Mexico", "United States", "USA", (-90.0, 27.5)),
    ("Japan", "Japan", "JPN", (140.5, 34.5)),
    ("Persian Gulf (UAE)", "United Arab", "ARE", (53.5, 25.0)),
    ("Strait of Malacca (Malaysia)", "Malaysia", "MYS", (100.3, 3.9)),
]


@pytest.mark.parametrize("name,q,iso,pt", REGIONS, ids=[r[0] for r in REGIONS])
def test_region_end_to_end(h, name, q, iso, pt):
    # 1 jurisdiction search
    s = requests.get(f"{BASE}/api/jurisdictions", params={"q": q, "zone_type": "eez", "active": "true"}, headers=h, timeout=30).json()
    codes = [z["code"] for z in s["zones"]]
    assert any(z["country"] == iso for z in s["zones"]), f"search for {q} returned {codes}"
    zone = next(z for z in s["zones"] if z["country"] == iso and z["code"] == f"{iso}-EEZ")
    assert zone["provenance"] == "REFERENCE" and zone.get("official") is not True
    # 2 geometry returned + coordinates valid
    g = requests.get(f"{BASE}/api/jurisdictions/{zone['id']}/geometry", params={"detail": "low"}, headers=h, timeout=30).json()
    assert g["geometry"]["type"] in ("Polygon", "MultiPolygon")
    coords = [c for poly in (g["geometry"]["coordinates"] if g["geometry"]["type"] == "MultiPolygon" else [g["geometry"]["coordinates"]]) for ring in poly for c in ring]
    assert coords and all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in coords)
    # 3 point lookup lands in this country's zone
    lk = requests.post(f"{BASE}/api/jurisdictions/lookup", json={"lon": pt[0], "lat": pt[1]}, headers=h, timeout=30).json()
    assert lk["inside_zone"] and lk["country_code"] == iso, lk
    assert "not a legal determination" in lk["disclaimer"]
    # 4 AOI from zone → Sentinel bbox + AIS bboxes (no India assumption)
    a = requests.post(f"{BASE}/api/aoi/select", json={"kind": "zone", "zone_id": zone["id"]}, headers=h, timeout=60).json()
    w, s_, e, n = a["bbox"]
    assert w < e and s_ < n and a["sentinel"]["bbox"] == a["bbox"]
    assert 1 <= len(a["ais_bboxes_swne"]) <= 12 and all(b[0] < b[2] and b[1] < b[3] for b in a["ais_bboxes_swne"])
    st = requests.get(f"{BASE}/api/ais/status", headers=h, timeout=30).json()
    assert st["coverage_mode"] == "manual" and st["coverage_bbox"] == a["ais_bboxes_swne"], "AIS coverage must follow the AOI"
    # 5 real Sentinel-1 STAC search over a 0.5° window inside the probe point (last 30 days)
    end = datetime.now(timezone.utc)
    r = requests.get(f"{BASE}/api/sentinel/search", params={"bbox": f"{pt[0] - .25},{pt[1] - .25},{pt[0] + .25},{pt[1] + .25}", "start": (end - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 5}, headers=h, timeout=90)
    assert r.status_code == 200, r.text[:200]
    res = r.json()
    assert "Planetary Computer" in res["source"]
    for sc in res["scenes"]:
        assert sc["stac_id"].startswith("S1") and sc["datetime"]


def test_international_waters_custom_aoi(h):
    geom = {"type": "Polygon", "coordinates": [[[-40, 20], [-38, 20], [-38, 22], [-40, 22], [-40, 20]]]}  # mid-Atlantic, no EEZ
    lk = requests.post(f"{BASE}/api/jurisdictions/lookup", json={"geometry": geom}, headers=h, timeout=30).json()
    assert lk["inside_zone"] is False and lk["country_code"] is None and lk["zone"] is None and "No reference maritime zone" in lk["note"]
    a = requests.post(f"{BASE}/api/aoi/select", json={"kind": "custom", "geometry": geom, "name": "mid-Atlantic test"}, headers=h, timeout=60).json()
    assert a["provenance"] == "USER-DEFINED" and a["jurisdiction"]["inside_zone"] is False and len(a["ais_bboxes_swne"]) >= 1
    r = requests.get(f"{BASE}/api/sentinel/search", params={"bbox": "-40,20,-38,22", "start": "2026-08-01T00:00:00Z", "end": "2026-09-10T00:00:00Z", "limit": 3}, headers=h, timeout=90)
    assert r.status_code == 200  # may legitimately return 0 scenes — never invented


def test_large_aoi_split_and_antimeridian_and_viewport(h):
    big = {"type": "Polygon", "coordinates": [[[30, -30], [100, -30], [100, 25], [30, 25], [30, -30]]]}  # whole Indian Ocean
    a = requests.post(f"{BASE}/api/aoi/select", json={"kind": "custom", "geometry": big, "name": "Indian Ocean"}, headers=h, timeout=60).json()
    assert 2 <= len(a["ais_bboxes_swne"]) <= 12
    fj = requests.get(f"{BASE}/api/jurisdictions/geojson", params={"bbox": "170,-20,180,20", "detail": "low"}, headers=h, timeout=60).json()  # Fiji / antimeridian edge
    assert fj["type"] == "FeatureCollection" and all(f["properties"]["detail"] == "low" for f in fj["features"])
    world = requests.get(f"{BASE}/api/jurisdictions/geojson", params={"bbox": "-180,-85,180,85", "limit": 5}, headers=h, timeout=60).json()
    assert world["count"] <= 5 and world["truncated"] is True
    assert requests.get(f"{BASE}/api/jurisdictions/geojson", headers=h, timeout=30).status_code == 400  # never the whole world
    empty = requests.get(f"{BASE}/api/jurisdictions", params={"q": "zzqx-no-such-country"}, headers=h, timeout=30).json()
    assert empty["total"] == 0 and empty["zones"] == []
    multi = requests.get(f"{BASE}/api/jurisdictions/IDN-EEZ/geometry", headers=h, timeout=60).json()  # Indonesia: MultiPolygon archipelago
    assert multi["geometry"]["type"] == "MultiPolygon"


def test_sih_preset_and_clear(h):
    a = requests.post(f"{BASE}/api/aoi/select", json={"kind": "preset", "preset": "sih_mumbai"}, headers=h, timeout=60).json()
    assert a["provenance"] == "PRESET" and a["jurisdiction"]["country_code"] == "IND"
    c = requests.delete(f"{BASE}/api/aoi", headers=h, timeout=30).json()
    assert c["cleared"] and c["ais_coverage"]["mode"] == "default"
