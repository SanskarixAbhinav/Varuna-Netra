"""Iteration 14 backend tests: dark-vessel scan, evidence vault, ICG district recipients."""
import os
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN = ("shawpriyanshu950@gmail.com", os.environ.get("TEST_ADMIN_PASSWORD"))
ANALYST = ("analyst@sentinelmar.demo", os.environ.get("TEST_ANALYST_PASSWORD"))
SUPERVISOR = ("supervisor@sentinelmar.demo", os.environ.get("TEST_SUPERVISOR_PASSWORD"))


def _login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


# Dark-vessel scan
class TestDarkVessel:
    def test_scan_on_dark_spot_detector_case(self):
        tok = _login(*ANALYST)
        cases = requests.get(f"{API}/cases", headers=_h(tok), timeout=30).json()
        target = next((c for c in cases if c.get("source") == "dark_spot_detector"), None)
        assert target, "no case with source=dark_spot_detector found"
        case_id = target["id"]
        r = requests.post(f"{API}/cases/{case_id}/dark-vessels/scan?radius_km=150", headers=_h(tok), timeout=120)
        assert r.status_code == 201, f"expected 201 got {r.status_code}: {r.text[:400]}"
        data = r.json()
        assert "targets" in data and "dark_count" in data
        assert "bright_targets_total" in data and "ais_fixes_checked" in data
        assert "disclaimer" in data
        assert isinstance(data["targets"], list) and len(data["targets"]) > 0, "expected targets > 0"
        for t in data["targets"]:
            assert "lat" in t and "lon" in t and "bbox" in t and "snr" in t
            assert "est_length_m" in t and "dark_candidate" in t
            if t["dark_candidate"]:
                assert "trajectory" in t and len(t["trajectory"]) == 4
                assert "escape_heading_deg" in t
        # GET latest returns the same scan
        r2 = requests.get(f"{API}/cases/{case_id}/dark-vessels", headers=_h(tok), timeout=30)
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2.get("id") == data.get("id")
        assert d2.get("dark_count") == data["dark_count"]

    def test_scan_on_seeded_case_400(self):
        tok = _login(*ANALYST)
        # Find seeded case
        cases = requests.get(f"{API}/cases", headers=_h(tok), timeout=30).json()
        seeded = next((c for c in cases if c.get("case_number") == "SPL-20260610-001"), None)
        assert seeded, "seeded case SPL-20260610-001 not found"
        r = requests.post(f"{API}/cases/{seeded['id']}/dark-vessels/scan?radius_km=40", headers=_h(tok), timeout=60)
        assert r.status_code == 400
        assert "quicklook" in r.text.lower() or "sar" in r.text.lower()

    def test_scan_unauthenticated_401(self):
        # Grab any dark case id via admin
        tok = _login(*ADMIN)
        cases = requests.get(f"{API}/cases", headers=_h(tok), timeout=30).json()
        target = next((c for c in cases if c.get("source") == "dark_spot_detector"), None)
        assert target
        r = requests.post(f"{API}/cases/{target['id']}/dark-vessels/scan?radius_km=40", timeout=30)
        assert r.status_code == 401

    def test_supervisor_allowed(self):
        tok = _login(*SUPERVISOR)
        cases = requests.get(f"{API}/cases", headers=_h(tok), timeout=30).json()
        target = next((c for c in cases if c.get("source") == "dark_spot_detector"), None)
        assert target
        r = requests.get(f"{API}/cases/{target['id']}/dark-vessels", headers=_h(tok), timeout=30)
        assert r.status_code == 200


# Evidence Vault
class TestVault:
    def test_vault_wakashio(self):
        tok = _login(*ANALYST)
        r = requests.get(f"{API}/archive?q=wakashio", headers=_h(tok), timeout=30)
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) >= 1
        entry_id = rows[0]["id"]
        v = requests.get(f"{API}/archive/{entry_id}/vault", headers=_h(tok), timeout=30)
        assert v.status_code == 200
        d = v.json()
        assert d["evidence"]["ruling"]
        assert len(d["evidence"]["sources"]) >= 1
        assert len(d["frames"]) == 25
        assert "RECONSTRUCTED" in d["note"]

    def test_vault_unknown_404(self):
        tok = _login(*ANALYST)
        r = requests.get(f"{API}/archive/nonexistent-id-xyz/vault", headers=_h(tok), timeout=30)
        assert r.status_code == 404


# ICG district recipients
class TestIcgRecipients:
    def test_admin_can_set_recipients(self):
        tok = _login(*ADMIN)
        payload = {"recipients": ["Desk.Kochi@example.in", "bad-email", "ops@example.in"]}
        r = requests.put(f"{API}/icg/districts/ICG-KOC", headers=_h(tok), json=payload, timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["recipients"] == ["desk.kochi@example.in", "ops@example.in"]
        # visible in list
        lst = requests.get(f"{API}/icg/districts", headers=_h(tok), timeout=30).json()
        row = next((x for x in lst["districts"] if x["code"] == "ICG-KOC"), None)
        assert row and row["recipients"] == ["desk.kochi@example.in", "ops@example.in"]

    def test_analyst_forbidden(self):
        tok = _login(*ANALYST)
        r = requests.put(f"{API}/icg/districts/ICG-KOC", headers=_h(tok), json={"recipients": ["x@y.com"]}, timeout=30)
        assert r.status_code == 403
