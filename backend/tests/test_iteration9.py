"""Iteration 9 backend tests: Gazetteer, Historical Archive, Playbook, Prosecution Export, Verify."""
import hashlib
import io
import json
import os
import zipfile

import pytest
import requests

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].split("\n")[0].strip()).rstrip("/")
API = f"{BASE_URL}/api"

ADMIN = ("shawpriyanshu950@gmail.com", os.environ["TEST_ADMIN_PASSWORD"])
ANALYST = ("analyst@sentinelmar.demo", os.environ["TEST_ANALYST_PASSWORD"])
SUPERVISOR = ("supervisor@sentinelmar.demo", os.environ["TEST_SUPERVISOR_PASSWORD"])
CASE_NUMBER = "SPL-20260610-001"


def login(email, pw):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def h(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def tokens():
    return {"admin": login(*ADMIN), "analyst": login(*ANALYST), "supervisor": login(*SUPERVISOR)}


@pytest.fixture(scope="module")
def case_id(tokens):
    r = requests.get(f"{API}/cases", headers=h(tokens["analyst"]), params={"case_number": CASE_NUMBER}, timeout=15)
    assert r.status_code == 200
    data = r.json()
    items = data.get("items") if isinstance(data, dict) else data
    for c in items:
        if c["case_number"] == CASE_NUMBER:
            return c["id"]
    pytest.skip(f"case {CASE_NUMBER} not found")


# ---------------- Gazetteer ----------------
class TestGazetteer:
    def test_search_bombay(self, tokens):
        r = requests.get(f"{API}/gazetteer/search", headers=h(tokens["analyst"]), params={"q": "bombay"}, timeout=15)
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) > 0
        assert "bombay high" in results[0]["name"].lower()
        assert "match" in results[0] and results[0]["match"] > 55

    def test_search_typo(self, tokens):
        r = requests.get(f"{API}/gazetteer/search", headers=h(tokens["analyst"]), params={"q": "bombay hgih"}, timeout=15)
        assert r.status_code == 200
        assert any("bombay" in x["name"].lower() for x in r.json())

    def test_search_kg_basin(self, tokens):
        r = requests.get(f"{API}/gazetteer/search", headers=h(tokens["analyst"]), params={"q": "kg basin"}, timeout=15)
        assert r.status_code == 200
        names = [x["name"].lower() for x in r.json()]
        assert any("krishna-godavari" in n or "kg" in n for n in names)

    def test_search_netherlands_returns_eez(self, tokens):
        r = requests.get(f"{API}/gazetteer/search", headers=h(tokens["analyst"]), params={"q": "netherlands", "limit": 20}, timeout=15)
        assert r.status_code == 200
        results = r.json()
        types = {x["type"] for x in results}
        # Should include country entry; EEZ inclusion depends on active jurisdictions
        assert "country" in types or "eez" in types

    def test_types(self, tokens):
        r = requests.get(f"{API}/gazetteer/types", headers=h(tokens["analyst"]), timeout=15)
        assert r.status_code == 200
        j = r.json()
        for t in ["oil_field", "basin", "terminal", "shipping_lane", "country", "eez"]:
            assert t in j["types"]
        assert j["count"] >= 60

    def test_create_analyst_403(self, tokens):
        r = requests.post(f"{API}/gazetteer", headers=h(tokens["analyst"]),
                          json={"name": "TEST_x", "type": "oil_field", "bbox": [0, 0, 1, 1]}, timeout=15)
        assert r.status_code == 403

    def test_create_admin_201_and_delete(self, tokens):
        r = requests.post(f"{API}/gazetteer", headers=h(tokens["admin"]),
                          json={"name": "TEST_iter9_asset", "type": "oil_field", "country": "TX",
                                "bbox": [10.0, 20.0, 11.0, 21.0]}, timeout=15)
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
        # delete admin-created ok
        dr = requests.delete(f"{API}/gazetteer/{aid}", headers=h(tokens["admin"]), timeout=15)
        assert dr.status_code == 200

    def test_create_invalid_type_400(self, tokens):
        r = requests.post(f"{API}/gazetteer", headers=h(tokens["admin"]),
                          json={"name": "TEST_bad", "type": "spaceship", "bbox": [0, 0, 1, 1]}, timeout=15)
        assert r.status_code == 400

    def test_create_bad_bbox_400(self, tokens):
        r = requests.post(f"{API}/gazetteer", headers=h(tokens["admin"]),
                          json={"name": "TEST_bad", "type": "oil_field", "bbox": [10, 20, 5, 21]}, timeout=15)
        assert r.status_code == 400

    def test_delete_seed_404(self, tokens):
        # find a seed entry
        r = requests.get(f"{API}/gazetteer/search", headers=h(tokens["admin"]), params={"q": "bombay"}, timeout=15)
        seed_id = None
        for row in r.json():
            if row.get("source") == "seed":
                seed_id = row["id"]
                break
        assert seed_id, "no seed row found"
        dr = requests.delete(f"{API}/gazetteer/{seed_id}", headers=h(tokens["admin"]), timeout=15)
        assert dr.status_code == 404


# ---------------- Archive ----------------
class TestArchive:
    def test_list_seeded(self, tokens):
        r = requests.get(f"{API}/archive", headers=h(tokens["analyst"]), timeout=15)
        assert r.status_code == 200
        rows = r.json()
        names = " | ".join(x["name"] for x in rows)
        assert "Deepwater Horizon" in names
        assert "X-Press Pearl" in names
        assert "Dawn Kanchipuram" in names
        seeds = [r for r in rows if r.get("source") == "seed"]
        assert len(seeds) >= 12

    def test_filter_chennai(self, tokens):
        r = requests.get(f"{API}/archive", headers=h(tokens["analyst"]), params={"q": "chennai"}, timeout=15)
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) >= 1
        assert any("chennai" in x["name"].lower() or "ennore" in x["name"].lower() or "chennai" in (x.get("cause") or "").lower() for x in rows)

    def test_create_analyst_403(self, tokens):
        r = requests.post(f"{API}/archive", headers=h(tokens["analyst"]),
                          json={"name": "TEST_a", "date": "2024-01-01", "lat": 0, "lon": 0}, timeout=15)
        assert r.status_code == 403

    def test_create_supervisor_201(self, tokens):
        r = requests.post(f"{API}/archive", headers=h(tokens["supervisor"]),
                          json={"name": "TEST_iter9_spill", "date": "2024-06-15", "lat": 10.0, "lon": 75.0,
                                "volume_tonnes": 100, "oil_type": "crude", "cause": "test", "country": "TEST"},
                          timeout=15)
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        # cleanup
        dr = requests.delete(f"{API}/archive/{eid}", headers=h(tokens["supervisor"]), timeout=15)
        assert dr.status_code == 200

    def test_create_bad_date_400(self, tokens):
        r = requests.post(f"{API}/archive", headers=h(tokens["supervisor"]),
                          json={"name": "TEST_bad", "date": "not-a-date", "lat": 0, "lon": 0}, timeout=15)
        assert r.status_code == 400

    def test_delete_seed_404(self, tokens):
        r = requests.get(f"{API}/archive", headers=h(tokens["supervisor"]), timeout=15)
        seed = next(x for x in r.json() if x.get("source") == "seed")
        dr = requests.delete(f"{API}/archive/{seed['id']}", headers=h(tokens["supervisor"]), timeout=15)
        assert dr.status_code == 404

    def test_precedents(self, tokens, case_id):
        r = requests.get(f"{API}/cases/{case_id}/precedents", headers=h(tokens["analyst"]), timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert "estimated_volume_tonnes" in j
        assert "weights" in j and set(j["weights"].keys()) == {"distance", "volume", "oil_type"}
        assert len(j["precedents"]) == 3
        for p in j["precedents"]:
            assert "similarity" in p and 0 <= p["similarity"] <= 1
            assert "distance_km" in p


# ---------------- Playbook ----------------
class TestPlaybook:
    def test_playbook(self, tokens, case_id):
        r = requests.get(f"{API}/cases/{case_id}/playbook", headers=h(tokens["analyst"]), timeout=30)
        assert r.status_code == 200, r.text
        pb = r.json()
        assert pb["advisory"] is True
        assert "playbook-rules" in pb["version"]
        assert "disclaimer" in pb
        inp = pb["inputs"]
        for k in ("area_km2", "estimated_volume_tonnes", "coast_distance_km", "depth_class", "sea_state", "drift_bearing_deg", "eta_to_coast_hours"):
            assert k in inp
        vol = inp["estimated_volume_tonnes"]
        for k in ("sheen", "thin", "thick"):
            assert k in vol
        # For 001, wind/current known → 4 tactical coordinates
        assert len(pb["tactical_coordinates"]) == 4
        assert len(pb["tiers"]) == 3
        tier2 = pb["tiers"][1]
        for k in ("dispersant", "in_situ_burning", "bioremediation"):
            assert k in tier2
            assert "suitable" in tier2[k] and "reason" in tier2[k]

    def test_playbook_unknown_404(self, tokens):
        r = requests.get(f"{API}/cases/does-not-exist/playbook", headers=h(tokens["analyst"]), timeout=15)
        assert r.status_code == 404

    def test_evidence_pdf_still_ok(self, tokens, case_id):
        r = requests.get(f"{API}/cases/{case_id}/evidence.pdf", headers=h(tokens["analyst"]), timeout=30)
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("application/pdf")


# ---------------- Prosecution Export & Verify ----------------
class TestProsecution:
    def test_export_analyst_403(self, tokens, case_id):
        r = requests.post(f"{API}/cases/{case_id}/prosecution-export", headers=h(tokens["analyst"]), timeout=60)
        assert r.status_code == 403

    def test_export_supervisor_zip(self, tokens, case_id):
        r = requests.post(f"{API}/cases/{case_id}/prosecution-export", headers=h(tokens["supervisor"]), timeout=90)
        assert r.status_code == 200, r.text
        assert r.headers.get("content-type") == "application/zip"
        assert r.headers.get("x-bundle-sha256")
        assert r.headers.get("x-export-id")
        bundle_hash = r.headers["x-bundle-sha256"]
        export_id = r.headers["x-export-id"]
        blob = r.content
        assert hashlib.sha256(blob).hexdigest() == bundle_hash

        z = zipfile.ZipFile(io.BytesIO(blob))
        names = set(z.namelist())
        for req in ["MANIFEST.json", "evidence.pdf", "case.json", "ais_fixes.json",
                    "correlation_results.json", "audit_history.json", "remediation_playbook.json"]:
            assert req in names, f"missing {req}"

        manifest = json.loads(z.read("MANIFEST.json"))
        # verify sha256 of each file
        for fname, meta in manifest["files"].items():
            actual = hashlib.sha256(z.read(fname)).hexdigest()
            assert actual == meta["sha256"], f"mismatch {fname}"

        # store for later tests
        pytest.iter9_bundle = blob
        pytest.iter9_bundle_hash = bundle_hash
        pytest.iter9_export_id = export_id

    def test_list_exports(self, tokens, case_id):
        r = requests.get(f"{API}/cases/{case_id}/prosecution-exports", headers=h(tokens["analyst"]), timeout=15)
        assert r.status_code == 200
        rows = r.json()
        assert any(x["id"] == pytest.iter9_export_id for x in rows)

    def test_audit_history_has_export(self, tokens, case_id):
        r = requests.get(f"{API}/cases/{case_id}/evidence", headers=h(tokens["analyst"]), timeout=15)
        assert r.status_code == 200
        actions = [a["action"] for a in r.json().get("audit_history", [])]
        assert "case.prosecution_exported" in actions

    def test_verify_by_hash_public(self):
        if not getattr(pytest, "iter9_bundle_hash", None):
            pytest.skip("export step skipped (demo dataset purged)")
        r = requests.get(f"{API}/verify/{pytest.iter9_bundle_hash}", timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert j["verified"] is True
        assert j["matched"] == "bundle"

    def test_verify_unknown_hash(self):
        r = requests.get(f"{API}/verify/deadbeef", timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert j["verified"] is False
        assert j["status"] == "unknown_hash"

    def test_verify_upload_ok(self):
        if not getattr(pytest, "iter9_bundle", None):
            pytest.skip("export step skipped (demo dataset purged)")
        files = {"file": ("bundle.zip", pytest.iter9_bundle, "application/zip")}
        r = requests.post(f"{API}/verify", files=files, timeout=30)
        assert r.status_code == 200
        j = r.json()
        assert j["status"] == "verified"
        assert all(c["ok"] for c in j["files"])

    def test_verify_tampered(self):
        if not getattr(pytest, "iter9_bundle", None):
            pytest.skip("export step skipped (demo dataset purged)")
        # rewrite case.json inside the zip
        buf = io.BytesIO()
        src = zipfile.ZipFile(io.BytesIO(pytest.iter9_bundle))
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == "case.json":
                    data = b'{"tampered": true}'
                z.writestr(info, data)
        files = {"file": ("tampered.zip", buf.getvalue(), "application/zip")}
        r = requests.post(f"{API}/verify", files=files, timeout=30)
        assert r.status_code == 200
        j = r.json()
        assert j["status"] == "tampered"
        case_check = next(c for c in j["files"] if c["file"] == "case.json")
        assert case_check["ok"] is False

    def test_verify_invalid_bundle(self):
        # random zip without MANIFEST
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("hello.txt", b"world")
        files = {"file": ("random.zip", buf.getvalue(), "application/zip")}
        r = requests.post(f"{API}/verify", files=files, timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert j["status"] == "invalid_bundle"
