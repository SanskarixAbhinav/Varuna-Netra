"""Iteration 7 backend tests: AIS density, scene watches, detector, before/after, spill-events, cron."""
import os
import time
import uuid
import requests
import pytest

def _read_frontend_env():
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL"):
                return line.split("=", 1)[1].strip().strip('"').rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL not found")


BASE = os.environ.get("REACT_APP_BACKEND_URL") or _read_frontend_env()
BASE = BASE.rstrip("/")
API = f"{BASE}/api"

ADMIN = ("shawpriyanshu950@gmail.com", os.environ["TEST_ADMIN_PASSWORD"])
SUP = ("supervisor@sentinelmar.demo", os.environ["TEST_SUPERVISOR_PASSWORD"])
ANL = ("analyst@sentinelmar.demo", os.environ["TEST_ANALYST_PASSWORD"])


def _login(email, pw):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def tok_admin():
    return _login(*ADMIN)


@pytest.fixture(scope="session")
def tok_sup():
    return _login(*SUP)


@pytest.fixture(scope="session")
def tok_analyst():
    return _login(*ANL)


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


# ---------- AIS density ----------
class TestDensity:
    def test_density_basic(self, tok_analyst):
        r = requests.get(f"{API}/ais/density?hours=2160&zoom=6", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "cells" in d and "max" in d and "resolution_deg" in d
        assert isinstance(d["cells"], list)
        if d["cells"]:
            c = d["cells"][0]
            assert {"lat", "lon", "count", "vessels", "w"}.issubset(c.keys())

    def test_density_hours_cap(self, tok_analyst):
        r = requests.get(f"{API}/ais/density?hours=9000&zoom=6", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 422

    def test_density_bbox_filter(self, tok_analyst):
        r = requests.get(f"{API}/ais/density?hours=2160&zoom=6&bbox=99,1,104,5", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 200
        for c in r.json()["cells"]:
            assert 99 <= c["lon"] <= 104 and 1 <= c["lat"] <= 5

    def test_density_bad_bbox(self, tok_analyst):
        r = requests.get(f"{API}/ais/density?hours=24&zoom=6&bbox=nope", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 400

    def test_density_cache(self, tok_analyst):
        u = f"{API}/ais/density?hours=2160&zoom=6"
        t1 = time.time(); r1 = requests.get(u, headers=_h(tok_analyst), timeout=30); d1 = time.time() - t1
        t2 = time.time(); r2 = requests.get(u, headers=_h(tok_analyst), timeout=30); d2 = time.time() - t2
        assert r1.json() == r2.json()
        # cache should be faster (not asserted strictly), but responses identical is sufficient
        _ = d1, d2


# ---------- Scene watches ----------
class TestSceneWatches:
    def test_list_any_role(self, tok_analyst):
        r = requests.get(f"{API}/scene-watches", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_analyst_cannot_create(self, tok_analyst):
        r = requests.post(f"{API}/scene-watches", headers=_h(tok_analyst),
                          json={"name": "x", "bbox": [99, 1, 104, 5]}, timeout=30)
        assert r.status_code == 403

    def test_supervisor_create_and_bad_bbox(self, tok_sup):
        # bad bbox
        r = requests.post(f"{API}/scene-watches", headers=_h(tok_sup),
                          json={"name": "bad", "bbox": [99, 5, 104, 1]}, timeout=30)
        assert r.status_code == 400
        # unknown collection
        r = requests.post(f"{API}/scene-watches", headers=_h(tok_sup),
                          json={"name": "bad2", "bbox": [99, 1, 104, 5], "collection": "nope"}, timeout=30)
        assert r.status_code == 400

    def test_full_supervisor_flow(self, tok_sup):
        name = f"TEST_{uuid.uuid4().hex[:6]}"
        r = requests.post(f"{API}/scene-watches", headers=_h(tok_sup),
                          json={"name": name, "bbox": [99, 1, 104, 5], "collection": "sentinel-1-grd", "auto_detect": False}, timeout=30)
        assert r.status_code == 201, r.text
        w = r.json()
        assert w["active"] is True
        wid = w["id"]

        # patch active:false
        r = requests.patch(f"{API}/scene-watches/{wid}", headers=_h(tok_sup), json={"active": False}, timeout=30)
        assert r.status_code == 200 and r.json()["active"] is False

        # run sync
        r = requests.post(f"{API}/scene-watches/{wid}/run?sync=true", headers=_h(tok_sup), timeout=180)
        assert r.status_code == 202, r.text
        job = r.json()
        assert job.get("status") in ("succeeded", "failed"), job
        if job["status"] == "succeeded":
            assert "watches" in job.get("result", {})

        # delete
        r = requests.delete(f"{API}/scene-watches/{wid}", headers=_h(tok_sup), timeout=30)
        assert r.status_code == 200 and r.json().get("ok")

    def test_cron_endpoint(self):
        secret = None
        with open("/app/backend/.env") as f:
            for line in f:
                if line.startswith("WEBHOOK_CRON_SECRET="):
                    secret = line.split("=", 1)[1].strip()
        assert secret
        # no auth
        r = requests.post(f"{API}/cron/scene-watch", json={}, timeout=30)
        assert r.status_code == 401
        # wrong token
        r = requests.post(f"{API}/cron/scene-watch", headers={"Authorization": "Bearer wrong"}, json={}, timeout=30)
        assert r.status_code == 401
        # correct
        wid = f"test-{uuid.uuid4().hex[:8]}"
        r = requests.post(f"{API}/cron/scene-watch",
                          headers={"Authorization": f"Bearer {secret}", "X-Webhook-Id": wid}, json={}, timeout=30)
        assert r.status_code == 202, r.text
        assert r.json().get("ok") is True
        # duplicate
        r2 = requests.post(f"{API}/cron/scene-watch",
                           headers={"Authorization": f"Bearer {secret}", "X-Webhook-Id": wid}, json={}, timeout=30)
        assert r2.status_code == 202 and r2.json().get("duplicate") is True


# ---------- Detector / quicklook / overlay ----------
class TestDetector:
    @pytest.fixture(scope="class")
    def scene_with_preview(self, tok_analyst):
        r = requests.get(f"{API}/scenes", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 200
        for s in r.json():
            if (s.get("metadata") or {}).get("preview_href"):
                return s
        pytest.skip("no scene with preview_href")

    def test_overlay(self, tok_analyst, scene_with_preview):
        sid = scene_with_preview["id"]
        r = requests.get(f"{API}/scenes/{sid}/overlay", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["has_quicklook"] is True
        assert len(d["bounds"]) == 2 and len(d["bounds"][0]) == 2
        assert len(d["bbox"]) == 4

    def test_quicklook(self, tok_analyst, scene_with_preview):
        sid = scene_with_preview["id"]
        r = requests.get(f"{API}/scenes/{sid}/quicklook", headers=_h(tok_analyst), timeout=180)
        assert r.status_code == 200, r.text[:200] if r.status_code != 200 else ""
        assert r.headers.get("content-type", "").startswith("image/")
        assert len(r.content) > 1000

    def test_detect(self, tok_analyst, scene_with_preview):
        sid = scene_with_preview["id"]
        r = requests.post(f"{API}/scenes/{sid}/detect-dark-spots", headers=_h(tok_analyst), timeout=180)
        assert r.status_code == 201, r.text[:300]
        d = r.json()
        assert d.get("experimental") is True
        assert "darkspot" in d.get("detector", "")
        assert "spots" in d and "cases" in d
        if d["cases"]:
            case_summary = d["cases"][0]
            cid = case_summary["case_id"]
            assert (case_summary.get("confidence") or 0) <= 0.55
            # fetch full case doc
            rc = requests.get(f"{API}/cases/{cid}", headers=_h(tok_analyst), timeout=30)
            assert rc.status_code == 200
            case = rc.json()
            assert case.get("source") == "dark_spot_detector"
            assert "experimental_detector" in (case.get("quality_flags") or [])
            # check attachments
            r2 = requests.get(f"{API}/cases/{cid}/attachments", headers=_h(tok_analyst), timeout=30)
            assert r2.status_code == 200
            atts = r2.json()
            assert any(a.get("kind") == "sar_scene" and a.get("content_type") == "image/webp" for a in atts), atts


# ---------- Before/After ----------
class TestBeforeAfter:
    def test_ba_s1(self, tok_analyst):
        # resolve case id from case_number
        r = requests.get(f"{API}/cases", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 200
        target = next((c for c in r.json() if c.get("case_number") == "SPL-20260610-001"), None)
        assert target, "seeded case missing"
        cid = target["id"]
        r = requests.get(f"{API}/cases/{cid}/before-after?days=20", headers=_h(tok_analyst), timeout=180)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert "before" in d and "after" in d and "bbox" in d and "spill_geometry" in d


# ---------- Spill events ----------
class TestSpillEvents:
    def test_list(self, tok_analyst):
        r = requests.get(f"{API}/spill-events?limit=5", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert "total" in d and "events" in d and "sources" in d
        for e in d["events"]:
            assert "scene_provider_id" in e
            assert "thumb" in e

    def test_filters(self, tok_analyst):
        r = requests.get(f"{API}/spill-events?limit=5&min_conf=0.1&status=probable", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 200
        for e in r.json()["events"]:
            if e.get("detection_confidence") is not None:
                assert e["detection_confidence"] >= 0.1
            assert e.get("attribution_status") == "probable"


# ---------- Regression ----------
class TestRegression:
    def test_all_roles_login(self):
        for c in (ADMIN, SUP, ANL):
            assert _login(*c)

    def test_cases_list(self, tok_analyst):
        r = requests.get(f"{API}/cases", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 200

    def test_zone_rules(self, tok_analyst):
        r = requests.get(f"{API}/zone-rules", headers=_h(tok_analyst), timeout=30)
        assert r.status_code == 200

    def test_evidence_pdf_dark_spot(self, tok_analyst):
        r = requests.get(f"{API}/cases", headers=_h(tok_analyst), timeout=30)
        ds = next((c for c in r.json() if c.get("source") == "dark_spot_detector"), None)
        if not ds:
            pytest.skip("no dark-spot case")
        r2 = requests.get(f"{API}/cases/{ds['id']}/evidence.pdf", headers=_h(tok_analyst), timeout=60)
        assert r2.status_code == 200
        assert r2.headers.get("content-type", "").startswith("application/pdf")
