"""Iteration 15 refactor regression tests.

Covers PDF sections, playbook tiers/tactical, dark-vessel scan for
30575a62-... and the seeded case 400 behaviour.
"""
import io
import os
import pytest
import requests
import fitz  # pymupdf

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_EMAIL = "shawpriyanshu950@gmail.com"
ADMIN_PW = os.environ.get("TEST_ADMIN_PASSWORD", "Admin#2026")
ANALYST_EMAIL = "analyst@sentinelmar.demo"
ANALYST_PW = os.environ.get("TEST_ANALYST_PASSWORD", "Analyst#2026")

DARK_CASE_ID = "30575a62-e6d4-429f-a672-32b9221707ca"
VULN_CASE_ID = "1a87e9ad-329e-4023-80c7-8d4c01ebf336"
SEEDED_CASE_NO = "SPL-20260610-001"


@pytest.fixture(scope="module")
def seeded_case_id(analyst_headers):
    r = requests.get(f"{BASE_URL}/api/cases", headers=analyst_headers, timeout=15)
    r.raise_for_status()
    for c in r.json():
        if c.get("case_number") == SEEDED_CASE_NO:
            return c["id"]
    pytest.skip(f"seeded case {SEEDED_CASE_NO} not found")


@pytest.fixture(scope="module")
def analyst_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": ANALYST_EMAIL, "password": ANALYST_PW},
                      timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def analyst_headers(analyst_token):
    return {"Authorization": f"Bearer {analyst_token}"}


# ---------- PDF report ----------
REQUIRED_SECTIONS = [
    "1. Case summary",
    "2.",  # any section 2..7 header may vary but at least 7 must exist
    "7. Audit history",
    "9. Remediation playbook",
    "10. Shoreline vulnerability",
]


@pytest.mark.parametrize("case_key", ["seeded", VULN_CASE_ID])
def test_evidence_pdf_valid_and_contains_sections(case_key, analyst_headers, seeded_case_id):
    case_id = seeded_case_id if case_key == "seeded" else case_key
    r = requests.get(f"{BASE_URL}/api/cases/{case_id}/evidence.pdf",
                     headers=analyst_headers, timeout=60)
    assert r.status_code == 200, r.text[:400]
    assert r.headers.get("content-type", "").startswith("application/pdf")
    pdf = fitz.open(stream=r.content, filetype="pdf")
    assert pdf.page_count >= 1
    text = "\n".join(p.get_text() for p in pdf)
    for s in ["1. Case summary", "7. Audit history",
              "9. Remediation playbook", "10. Shoreline vulnerability"]:
        assert s in text, f"missing section '{s}' in PDF for {case_id}"


# ---------- Playbook ----------
def test_playbook_tiers_and_tactical(analyst_headers, seeded_case_id):
    r = requests.get(f"{BASE_URL}/api/cases/{seeded_case_id}/playbook",
                     headers=analyst_headers, timeout=30)
    assert r.status_code == 200, r.text[:400]
    js = r.json()
    tiers = js.get("tiers") or []
    assert len(tiers) >= 3
    t3 = tiers[2]
    assert t3.get("tier") == 3
    # dispersant/in_situ_burning/bioremediation live in tier 2 (chemical & biological treatment)
    t_chem = next((t for t in tiers if "dispersant" in t), None)
    assert t_chem is not None, f"no tier has dispersant key; keys={[list(t.keys()) for t in tiers]}"
    for k in ("dispersant", "in_situ_burning", "bioremediation"):
        assert k in t_chem
    tc = js.get("tactical_coordinates")
    assert isinstance(tc, list)
    assert "inputs" in js
    assert "eta_to_coast_hours" in js["inputs"]


def test_playbook_vuln_case(analyst_headers):
    r = requests.get(f"{BASE_URL}/api/cases/{VULN_CASE_ID}/playbook",
                     headers=analyst_headers, timeout=30)
    assert r.status_code == 200
    js = r.json()
    assert len(js.get("tiers", [])) >= 3


# ---------- Dark-vessel scan ----------
def test_dark_scan_returns_201_with_targets(analyst_headers):
    r = requests.post(
        f"{BASE_URL}/api/cases/{DARK_CASE_ID}/dark-vessels/scan",
        params={"radius_km": 150},
        headers=analyst_headers,
        timeout=90,
    )
    assert r.status_code == 201, r.text[:400]
    js = r.json()
    assert js.get("dark_count", 0) > 0
    darks = [t for t in js.get("targets", []) if t.get("dark_candidate")]
    assert len(darks) > 0
    for t in darks:
        assert isinstance(t.get("trajectory"), list) and len(t["trajectory"]) >= 2
        assert "escape_heading_deg" in t


def test_dark_scan_seeded_case_400(analyst_headers, seeded_case_id):
    r = requests.post(
        f"{BASE_URL}/api/cases/{seeded_case_id}/dark-vessels/scan",
        params={"radius_km": 150},
        headers=analyst_headers,
        timeout=30,
    )
    assert r.status_code == 400, r.text[:200]
