"""
Iteration 11 backend regression: post-refactor behavior of
- csv_ingest.rows_to_positions (via /api/ais/csv/preview + /api/ais/csv/ingest)
- gapfill.fill_gaps + correlate (via /api/cases/{id}/correlate?sync=true&fill_gaps=true)
Behaviour must be identical to iteration 10.
"""
import os
import time
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests
from dotenv import dotenv_values


def _read_base_url():
    if os.environ.get("REACT_APP_BACKEND_URL"):
        return os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
    env = dotenv_values("/app/frontend/.env")
    return env["REACT_APP_BACKEND_URL"].rstrip("/")


BASE = _read_base_url()
ANALYST_PW = os.environ.get("TEST_ANALYST_PASSWORD", "Analyst#2026")


@pytest.fixture(scope="module")
def analyst_token():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"email": "analyst@sentinelmar.demo", "password": ANALYST_PW},
                      timeout=60)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def analyst_headers(analyst_token):
    return {"Authorization": f"Bearer {analyst_token}"}


# -------------------- CSV ingest --------------------
class TestCsvIngest:
    HEADER = "MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,VesselName\n"

    def _csv(self):
        """Generate CSV with unique MMSI + timestamps, plus one row with empty MMSI (row-level error)."""
        base_mmsi = 900000000 + int(time.time()) % 1000000
        t0 = datetime.now(timezone.utc) - timedelta(days=3)
        rows = []
        for i in range(4):
            ts = (t0 + timedelta(minutes=i * 5)).strftime("%Y-%m-%dT%H:%M:%S")
            rows.append(f"{base_mmsi},{ts},53.{500 + i},3.{800 + i},10.{i},95,95,TEST_ITER11_{uuid.uuid4().hex[:6]}")
        # bad row: empty MMSI
        bad_ts = (t0 + timedelta(minutes=99)).strftime("%Y-%m-%dT%H:%M:%S")
        rows.append(f",{bad_ts},53.55,3.85,10,95,95,BAD_ROW")
        return (self.HEADER + "\n".join(rows) + "\n").encode()

    def test_preview_detects_mapping(self, analyst_headers):
        csv_bytes = self._csv()
        r = requests.post(f"{BASE}/api/ais/csv/preview",
                          headers=analyst_headers,
                          files={"file": ("t.csv", csv_bytes, "text/csv")},
                          timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        m = data.get("detected_mapping") or data.get("mapping")
        assert m, f"no mapping in response: {data}"
        for k in ("mmsi", "timestamp", "lat", "lon"):
            assert k in m, f"missing {k} in mapping: {m}"
        assert m["mmsi"].lower() == "mmsi"
        assert m["timestamp"].lower() in ("basedatetime", "base_date_time")

    def test_ingest_returns_counts_and_row_errors(self, analyst_headers):
        csv_bytes = self._csv()
        r = requests.post(f"{BASE}/api/ais/csv/ingest",
                          headers=analyst_headers,
                          files={"file": ("t.csv", csv_bytes, "text/csv")},
                          timeout=30)
        assert r.status_code in (200, 201), r.text
        data = r.json()
        assert "inserted" in data
        assert "duplicates" in data
        errs = data.get("row_errors") or data.get("errors") or []
        assert data["inserted"] == 4, data
        assert data["duplicates"] == 0, data
        assert len(errs) >= 1
        assert any("mmsi" in (e.get("error") or "").lower() for e in errs)


# -------------------- Correlate w/ gap-fill --------------------
class TestCorrelateGapFill:
    def _find_case(self, headers, case_number):
        r = requests.get(f"{BASE}/api/cases", headers=headers, params={"limit": 200}, timeout=15)
        assert r.status_code == 200, r.text
        items = r.json().get("items", r.json()) if isinstance(r.json(), dict) else r.json()
        for c in items:
            if c.get("case_number") == case_number:
                return c
        pytest.skip(f"case {case_number} not seeded")

    def test_correlate_sync_with_gap_fill(self, analyst_headers):
        case = self._find_case(analyst_headers, "SPL-20260610-001")
        # sync + fill_gaps go in JSON body per CorrelateRequest schema
        r = requests.post(f"{BASE}/api/cases/{case['id']}/correlate",
                          headers={**analyst_headers, "content-type": "application/json"},
                          json={"sync": True, "params": {"fill_gaps": True}},
                          timeout=120)
        # endpoint always returns 202 (declared status_code), even when sync=True
        assert r.status_code == 202, r.text
        job = r.json()
        assert job["type"] == "correlate"
        assert job["status"] in ("succeeded", "failed", "running"), job
        assert job["status"] == "succeeded", f"correlate job failed: {job}"
        result = job.get("result") or {}
        # Full result_version stored in db carries gap_fill.version; job result is a summary.
        # Fetch candidates response (includes full result envelope) to verify.
        cr = requests.get(f"{BASE}/api/cases/{case['id']}/candidates",
                          headers=analyst_headers, timeout=30)
        assert cr.status_code == 200
        cand_response = cr.json()
        cands = cand_response.get("candidates") if isinstance(cand_response, dict) else cand_response
        assert isinstance(cands, list) and len(cands) > 0
        gap_fill_meta = cand_response.get("gap_fill") if isinstance(cand_response, dict) else None
        gfv = (gap_fill_meta or {}).get("version")
        assert gfv and "gapfill" in str(gfv), \
            f"gap_fill.version missing/malformed in candidates response: gap_fill={gap_fill_meta}"
        assert any(("gap_segments" in (c.get("evidence") or {})) for c in cands), \
            f"no candidate.evidence has 'gap_segments' key: sample evidence keys={list((cands[0].get('evidence') or {}).keys())}"
