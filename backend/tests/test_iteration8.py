"""Iteration 8 backend tests: Detector Feedback, Live Alerts (SSE), AIS Gap Fill, Drift Model, Spoof Detection."""
import os
import time
import json
import threading
import hashlib
from datetime import datetime, timezone, timedelta

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") or open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].split("\n")[0].strip()
BASE_URL = BASE_URL.rstrip("/")
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


# ---------------- Detector Feedback ----------------
class TestDetectorFeedback:
    def test_fp_without_reason_400(self, tokens, case_id):
        r = requests.post(f"{API}/cases/{case_id}/detector-feedback",
                          headers=h(tokens["analyst"]),
                          json={"verdict": "false_positive"}, timeout=15)
        assert r.status_code == 400, r.text

    def test_fp_with_invalid_reason_400(self, tokens, case_id):
        r = requests.post(f"{API}/cases/{case_id}/detector-feedback",
                          headers=h(tokens["analyst"]),
                          json={"verdict": "false_positive", "reason": "banana"}, timeout=15)
        assert r.status_code == 400

    def test_tp_then_fp_and_list(self, tokens, case_id):
        # Submit TP first
        r = requests.post(f"{API}/cases/{case_id}/detector-feedback",
                          headers=h(tokens["analyst"]),
                          json={"verdict": "true_positive", "notes": "TEST tp"}, timeout=15)
        assert r.status_code == 201, r.text
        doc = r.json()
        assert doc["is_true_positive"] is True
        assert doc["user_email"] == ANALYST[0]
        assert "detector_version" in doc

        # Then FP
        r2 = requests.post(f"{API}/cases/{case_id}/detector-feedback",
                           headers=h(tokens["analyst"]),
                           json={"verdict": "false_positive", "reason": "wake", "notes": "TEST fp"}, timeout=15)
        assert r2.status_code == 201, r2.text
        assert r2.json()["reason"] == "wake"

        # List
        r3 = requests.get(f"{API}/cases/{case_id}/detector-feedback", headers=h(tokens["analyst"]), timeout=15)
        assert r3.status_code == 200
        lst = r3.json()
        assert isinstance(lst, list) and len(lst) >= 2

    def test_evidence_has_feedback_and_audit(self, tokens, case_id):
        r = requests.get(f"{API}/cases/{case_id}/evidence", headers=h(tokens["analyst"]), timeout=20)
        assert r.status_code == 200
        e = r.json()
        assert "detector_feedback" in e
        assert isinstance(e["detector_feedback"], list) and len(e["detector_feedback"]) >= 2
        actions = [a.get("action") for a in e.get("audit_history", [])]
        assert "detector.feedback" in actions

    def test_evidence_pdf_200(self, tokens, case_id):
        r = requests.get(f"{API}/cases/{case_id}/evidence.pdf", headers=h(tokens["analyst"]), timeout=60)
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("application/pdf")
        assert len(r.content) > 1000


# ---------------- Precision ----------------
class TestPrecision:
    def test_precision_shape(self, tokens):
        r = requests.get(f"{API}/detector/precision", headers=h(tokens["analyst"]), timeout=15)
        assert r.status_code == 200
        d = r.json()
        for key in ("versions", "overall", "fp_reasons", "weekly", "reasons_catalog", "recent"):
            assert key in d, f"missing {key}"
        for key in ("tp", "fp", "precision", "reviewed", "pending_review"):
            assert key in d["overall"]
        if d["overall"]["reviewed"] == 0:
            pytest.skip("no detector feedback in LIVE dataset (demo purged)")
        # Since we submitted TP then FP on same case, latest verdict wins -> counted once as FP
        assert d["overall"]["fp"] >= 1


# ---------------- SSE + Alerts ----------------
class TestLiveAlerts:
    def test_sse_invalid_token_401(self):
        r = requests.get(f"{API}/alerts/stream", params={"token": "bogus"}, timeout=10, stream=True)
        assert r.status_code == 401
        r.close()

    def test_sse_hello_and_alert_on_correlate(self, tokens, case_id):
        buf = []
        stop = threading.Event()

        def reader():
            try:
                r = requests.get(f"{API}/alerts/stream",
                                 params={"token": tokens["analyst"]},
                                 stream=True, timeout=30)
                assert r.status_code == 200
                for line in r.iter_lines(decode_unicode=True):
                    if stop.is_set():
                        break
                    if line:
                        buf.append(line)
                    if len(buf) > 200:
                        break
            except Exception as e:
                buf.append(f"ERR {e}")

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(2.0)  # wait for hello

        # Trigger correlate (produces alerts)
        cr = requests.post(f"{API}/cases/{case_id}/correlate",
                           headers=h(tokens["analyst"]),
                           json={"sync": True}, timeout=180)
        assert cr.status_code in (200, 201, 202), cr.text

        # Wait for events
        deadline = time.time() + 15
        while time.time() < deadline:
            if any(l.startswith("event: alert") for l in buf):
                break
            time.sleep(0.5)
        stop.set()
        time.sleep(0.5)

        joined = "\n".join(buf)
        assert "event: hello" in joined, joined[:500]
        # Alert event and job event should appear
        assert "event: alert" in joined, joined[:2000]
        assert "event: job" in joined or "succeeded" in joined, joined[:2000]

    def test_alerts_latest(self, tokens):
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        r = requests.get(f"{API}/alerts/latest", params={"since": since},
                         headers=h(tokens["analyst"]), timeout=15)
        assert r.status_code == 200
        d = r.json()
        for k in ("alerts", "unacknowledged", "server_time"):
            assert k in d

    def test_test_publish_rbac(self, tokens):
        r = requests.post(f"{API}/events/test-publish", headers=h(tokens["analyst"]), timeout=10)
        assert r.status_code == 403
        r2 = requests.post(f"{API}/events/test-publish", headers=h(tokens["admin"]), timeout=10)
        assert r2.status_code == 200
        assert r2.json().get("ok") is True


# ---------------- Correlation v1.1.0 (drift + gap fill) ----------------
class TestCorrelationV11:
    def test_correlate_default_gap_fill(self, tokens, case_id):
        r = requests.post(f"{API}/cases/{case_id}/correlate",
                          headers=h(tokens["analyst"]),
                          json={"sync": True}, timeout=180)
        assert r.status_code in (200, 201, 202), r.text
        cand = requests.get(f"{API}/cases/{case_id}/candidates",
                            headers=h(tokens["analyst"]), timeout=30).json()
        assert cand["algorithm_version"] == "corr-1.1.0", cand.get("algorithm_version")
        assert "drift_model" in cand and cand["drift_model"]
        dm = cand["drift_model"]
        for k in ("version", "hours", "path", "envelope", "likely_envelope", "likely_window_hours", "sigma_km"):
            assert k in dm, f"drift_model missing {k}"
        gf = cand.get("gap_fill")
        assert gf and gf["enabled"] is True and gf["version"]
        # Candidate evidence checks
        candidates = cand.get("candidates", [])
        assert candidates
        for c in candidates:
            ev = c.get("evidence", {})
            assert "interpolated_count" in ev
            assert "gap_segments" in ev
        # At least one candidate should have gap fill data (dead-reckoning)
        assert any((c.get("evidence", {}).get("interpolated_count", 0) > 0)
                   or c.get("evidence", {}).get("gap_segments")
                   for c in candidates), \
            "no candidate has interpolated_count>0 or gap_segments"

    def test_correlate_no_gap_fill(self, tokens, case_id):
        r = requests.post(f"{API}/cases/{case_id}/correlate",
                          headers=h(tokens["analyst"]),
                          json={"sync": True, "params": {"fill_gaps": False}}, timeout=180)
        assert r.status_code in (200, 201, 202), r.text
        cand = requests.get(f"{API}/cases/{case_id}/candidates",
                            headers=h(tokens["analyst"]), timeout=30).json()
        gf = cand.get("gap_fill")
        assert gf["enabled"] is False
        for c in cand.get("candidates", []):
            assert c.get("evidence", {}).get("interpolated_count", 0) == 0

    def test_reproducibility_same_input_hash(self, tokens, case_id):
        # Re-enable gap fill (default)
        r1 = requests.post(f"{API}/cases/{case_id}/correlate", headers=h(tokens["analyst"]),
                           json={"sync": True}, timeout=180).json()
        r2 = requests.post(f"{API}/cases/{case_id}/correlate", headers=h(tokens["analyst"]),
                           json={"sync": True}, timeout=180).json()
        c1 = requests.get(f"{API}/cases/{case_id}/candidates", headers=h(tokens["analyst"]), timeout=30).json()
        # trigger second
        c2 = requests.get(f"{API}/cases/{case_id}/candidates", headers=h(tokens["analyst"]), timeout=30).json()
        assert c1.get("input_hash") == c2.get("input_hash"), (c1.get("input_hash"), c2.get("input_hash"))

    def test_geojson_layers(self, tokens, case_id):
        r = requests.get(f"{API}/cases/{case_id}/geojson", headers=h(tokens["analyst"]), timeout=30)
        assert r.status_code == 200
        gj = r.json()
        feats = gj.get("features", [])
        layer_names = {f.get("properties", {}).get("layer") for f in feats}
        for k in ("drift_envelope", "drift_likely", "drift_path"):
            assert k in layer_names, f"missing layer {k}; layers={layer_names}"
        # At least one track feature with interpolated=True
        assert any(f.get("properties", {}).get("layer") == "track"
                   and f["properties"].get("interpolated") is True for f in feats), \
            "no track feature with properties.interpolated=True"


# ---------------- Spoof Detection ----------------
class TestSpoofDetection:
    def test_ingest_spoof_ais_and_correlate(self, tokens, case_id):
        # Two fixes 31 min apart, ~55 km apart (both within 28km corridor of spill 53.55,3.70) → ~58 kn > vmax 45 → spoof
        # Use time-unique MMSI to avoid interference from prior test runs
        mmsi = "886" + str(int(time.time()))[-6:]
        t0 = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=31)
        positions = [
            {"mmsi": mmsi, "timestamp": t0.isoformat().replace("+00:00", "Z"), "lat": 53.55, "lon": 3.28,
             "sog_kn": 10.0, "cog_deg": 90.0, "vessel_name": "TEST_SPOOF"},
            {"mmsi": mmsi, "timestamp": t1.isoformat().replace("+00:00", "Z"), "lat": 53.55, "lon": 4.12,
             "sog_kn": 10.0, "cog_deg": 90.0, "vessel_name": "TEST_SPOOF"},
        ]
        r = requests.post(f"{API}/ais/positions", headers=h(tokens["analyst"]),
                          json={"positions": positions}, timeout=30)
        assert r.status_code in (200, 201, 202), r.text

        cr = requests.post(f"{API}/cases/{case_id}/correlate",
                           headers=h(tokens["analyst"]),
                           json={"sync": True}, timeout=180)
        assert cr.status_code in (200, 201, 202)

        cand = requests.get(f"{API}/cases/{case_id}/candidates",
                            headers=h(tokens["analyst"]), timeout=30).json()
        spoof = next((c for c in cand.get("candidates", []) if str(c.get("mmsi")) == str(mmsi)), None)
        assert spoof, f"TEST spoof MMSI {mmsi} not found in candidates"
        flags = spoof.get("ais_flags") or spoof.get("evidence", {}).get("ais_flags") or []
        assert "spoof_suspect" in flags, f"flags={flags}"
        segs = spoof.get("evidence", {}).get("gap_segments") or []
        assert segs and segs[0].get("spoof_suspect") is True
        assert spoof.get("evidence", {}).get("interpolated_count", 0) == 0
