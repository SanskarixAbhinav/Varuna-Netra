"""Iteration 20 verification tests: canonical AIS routes, Sentinel scene endpoints,
scene_status, attach-scene, dark-vessels/scan with real SAR AOI window, and detect 409 guard."""
import os
import re
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
assert BASE, "REACT_APP_BACKEND_URL missing"

ADMIN = ("shawpriyanshu950@gmail.com", "Admin#2026")
ANALYST = ("analyst@sentinelmar.demo", "Analyst#2026")
SUPERVISOR = ("supervisor@sentinelmar.demo", "Supervisor#2026")


def _login(email, pw):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, f"login {email} → {r.status_code}: {r.text[:200]}"
    tok = r.json().get("access_token") or r.json().get("token")
    if tok:
        s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


@pytest.fixture(scope="module")
def admin():
    return _login(*ADMIN)


@pytest.fixture(scope="module")
def analyst():
    return _login(*ANALYST)


# --- AIS canonical endpoints ---
class TestAISCanonical:
    def test_status_shape(self, analyst):
        r = analyst.get(f"{BASE}/api/ais/status", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["state"] in ("NOT_CONFIGURED", "CONNECTING", "CONNECTED", "LIVE", "RECONNECTING", "OFFLINE")
        if not d["configured"]:
            assert d["state"] == "NOT_CONFIGURED" and d["connected"] is False and d.get("reason") == "API key not configured"
        elif d["state"] == "LIVE":
            assert d["connected"] and d["positions_parsed"] > 0
        assert isinstance(d.get("coverage_bbox"), list)
        # coverage_bbox format is [S,W,N,E] per contract
        assert d.get("coverage_bbox_format") == "[S,W,N,E]"
        # No api_key field of any kind
        blob = str(d).lower()
        assert "api_key" not in blob and "aisstream_api_key" not in blob

    def test_vessels_shape(self, analyst):
        r = analyst.get(f"{BASE}/api/ais/vessels", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["source"] == "AISStream"
        assert d["mode"] == "live"
        assert d["state"] in ("NOT_CONFIGURED", "CONNECTING", "CONNECTED", "LIVE", "RECONNECTING", "OFFLINE")
        assert isinstance(d["vessels"], list) and d["count"] == len(d["vessels"])
        if not d["configured"]:
            assert d["vessels"] == []
        assert "indexed" in d and isinstance(d["indexed"], list)
        assert "indexed_count" in d
        blob = str(d).lower()
        assert "api_key" not in blob


# --- Sentinel endpoints ---
class TestSentinel:
    def test_latest(self, analyst):
        r = analyst.get(f"{BASE}/api/sentinel/latest", timeout=30)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert "scene" in d
        if d["scene"]:
            assert d["scene"]["provider_scene_id"].startswith("S1"), d["scene"]["provider_scene_id"]
            ss = d["scene_status"]
            assert ss["state"] in ("SAR_READY", "QUICKLOOK_GENERATING", "SAR_UNAVAILABLE")
            pytest.scene_id = d["scene"]["id"]
            pytest.provider_scene_id = d["scene"]["provider_scene_id"]

    def test_assets(self, analyst):
        sid = getattr(pytest, "scene_id", None)
        if not sid:
            pytest.skip("no scene from latest")
        r = analyst.get(f"{BASE}/api/sentinel/scenes/{sid}/assets", timeout=60)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert d["analysis_asset"] in ("vv", "vh"), d["analysis_asset"]
        # sar_asset_accessible: true expected for SAR_READY
        assert "sar_asset_accessible" in d
        # No signed URL or token leakage
        blob = str(d)
        assert not re.search(r"https?://[^ \"']*sig=", blob), "signed URL leaked"
        assert "sv=" not in blob.lower() or "signing" in blob.lower()  # signing note is allowed
        # scan raw assets urls: they may reference blob.core.windows.net but must not include se=/sig=/sv= query
        for a in d.get("assets", []) or []:
            u = str(a.get("href", ""))
            assert "sig=" not in u and "?sv=" not in u.lower(), f"leaked signed url: {u}"


# --- Cases: scene_status, attach-scene, dark-vessels ---
class TestCases:
    def test_case_scene_status_and_dark_scan(self, analyst):
        r = analyst.get(f"{BASE}/api/cases?limit=50", timeout=20)
        assert r.status_code == 200
        cases = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
        with_scene = next((c for c in cases if c.get("scene_id")), None)
        without_scene = next((c for c in cases if not c.get("scene_id")), None)
        pytest.cases_with = with_scene
        pytest.cases_without = without_scene

        if with_scene:
            cid = with_scene["id"]
            d = analyst.get(f"{BASE}/api/cases/{cid}", timeout=20).json()
            ss = d.get("scene_status") or {}
            assert ss.get("state") in ("SAR_READY", "QUICKLOOK_GENERATING", "SAR_UNAVAILABLE"), ss
            if ss.get("state") == "SAR_READY":
                # run real dark-vessel scan
                rr = analyst.post(f"{BASE}/api/cases/{cid}/dark-vessels/scan?radius_km=30", timeout=90)
                assert rr.status_code == 201, f"{rr.status_code}: {rr.text[:400]}"
                dd = rr.json()
                ai = dd.get("analysis_input") or {}
                assert ai.get("kind") == "sar_aoi_window", ai
                psid = dd.get("provider_scene_id") or ai.get("provider_scene_id") or ""
                assert psid.startswith("S1"), psid
                assert "ais_available" in dd
                assert isinstance(dd.get("dark_count"), int)

    def test_attach_scene_no_scene_case(self, analyst):
        w = getattr(pytest, "cases_without", None)
        if not w:
            pytest.skip("no case without scene_id")
        cid = w["id"]
        d = analyst.get(f"{BASE}/api/cases/{cid}", timeout=20).json()
        ss = d.get("scene_status") or {}
        assert ss.get("state") == "SAR_UNAVAILABLE", ss
        r = analyst.post(f"{BASE}/api/cases/{cid}/attach-scene", json={}, timeout=60)
        # either successful auto-attach or 409 with specific reason
        if r.status_code == 409:
            msg = (r.json().get("detail") or r.text or "").lower()
            assert "no sentinel" in msg or "no s1" in msg or "covers" in msg, msg
        else:
            assert r.status_code == 200, r.text[:300]
            assert (r.json().get("scene_status") or {}).get("state") in ("SAR_READY", "QUICKLOOK_GENERATING")


# --- POST /api/scenes/{id}/detect on manual scene → 409 ---
class TestDetectGuard:
    def test_detect_manual_scene_returns_409(self, admin):
        # find a manually-registered scene (no stac_collection)
        r = admin.get(f"{BASE}/api/scenes?limit=100", timeout=20)
        if r.status_code != 200:
            pytest.skip(f"scenes list not accessible: {r.status_code}")
        items = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
        manual = next((s for s in items if not (s.get("metadata") or {}).get("stac_collection")), None)
        if not manual:
            pytest.skip("no manually-registered scene to test 409 guard")
        rr = admin.post(f"{BASE}/api/scenes/{manual['id']}/detect", timeout=30)
        assert rr.status_code == 409, f"expected 409, got {rr.status_code}: {rr.text[:300]}"
