"""Iteration 3 backend tests: password reset, CSV upload, jurisdictions, vessel profile."""
import io
import os
import re
import time
import pytest
import requests

def _load_backend_url():
    if os.environ.get("REACT_APP_BACKEND_URL"):
        return os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip().strip('"').rstrip("/")
    except Exception:
        pass
    raise RuntimeError("REACT_APP_BACKEND_URL not found")

BASE_URL = _load_backend_url()
API = f"{BASE_URL}/api"

ADMIN = ("shawpriyanshu950@gmail.com", os.environ["TEST_ADMIN_PASSWORD"])
ANALYST = ("analyst@sentinelmar.demo", os.environ["TEST_ANALYST_PASSWORD"])
SUPERVISOR = ("supervisor@sentinelmar.demo", os.environ["TEST_SUPERVISOR_PASSWORD"])


def _login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login failed {email}: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def admin_h():
    return {"Authorization": f"Bearer {_login(*ADMIN)}"}


@pytest.fixture(scope="session")
def analyst_h():
    return {"Authorization": f"Bearer {_login(*ANALYST)}"}


# ---------- PASSWORD RESET ----------

class TestPasswordReset:
    def test_forgot_password_known_email(self):
        r = requests.post(f"{API}/auth/forgot-password", json={"email": ANALYST[0]}, timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert j["delivery"] == "logged"
        assert "message" in j

    def test_forgot_password_unknown_email_no_enumeration(self):
        r = requests.post(f"{API}/auth/forgot-password", json={"email": "no-such-user@example.test"}, timeout=15)
        assert r.status_code == 200
        assert r.json()["delivery"] == "none"

    def test_reset_requests_admin_only(self, admin_h, analyst_h):
        r_a = requests.get(f"{API}/auth/reset-requests", headers=analyst_h, timeout=15)
        assert r_a.status_code == 403

        r = requests.get(f"{API}/auth/reset-requests", headers=admin_h, timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body["email_configured"] is False
        assert isinstance(body["requests"], list)
        assert len(body["requests"]) >= 1
        row = next(x for x in body["requests"] if x["email"] == ANALYST[0])
        assert row["delivery"] == "logged"
        assert row["used"] is False
        assert row["link"] and "reset-password?token=" in row["link"]

    def test_reset_password_flow_and_reuse(self, admin_h):
        # request a fresh reset
        requests.post(f"{API}/auth/forgot-password", json={"email": ANALYST[0]}, timeout=15)
        rows = requests.get(f"{API}/auth/reset-requests", headers=admin_h, timeout=15).json()["requests"]
        row = next(x for x in rows if x["email"] == ANALYST[0] and not x["used"] and x.get("link"))
        token = re.search(r"token=([^&]+)", row["link"]).group(1)

        # short token → 422
        r_short = requests.post(f"{API}/auth/reset-password", json={"token": "abc", "new_password": "NewPass#2026"}, timeout=15)
        assert r_short.status_code == 422

        # wrong token → 400
        r_wrong = requests.post(f"{API}/auth/reset-password", json={"token": "x" * 40, "new_password": "NewPass#2026"}, timeout=15)
        assert r_wrong.status_code == 400

        # good token
        r_ok = requests.post(f"{API}/auth/reset-password", json={"token": token, "new_password": "NewPass#2026"}, timeout=15)
        assert r_ok.status_code == 200, r_ok.text
        assert r_ok.json()["ok"] is True
        assert r_ok.json()["email"] == ANALYST[0]

        # login with new password
        r_login = requests.post(f"{API}/auth/login", json={"email": ANALYST[0], "password": "NewPass#2026"}, timeout=15)
        assert r_login.status_code == 200

        # reuse token → 400
        r_reuse = requests.post(f"{API}/auth/reset-password", json={"token": token, "new_password": "AnotherPass#2026"}, timeout=15)
        assert r_reuse.status_code == 400

        # Restore analyst password via admin PATCH
        users = requests.get(f"{API}/users", headers=admin_h, timeout=15).json()
        analyst_user = next(u for u in users if u["email"] == ANALYST[0])
        r_restore = requests.patch(f"{API}/users/{analyst_user['id']}", json={"password": ANALYST[1]}, headers=admin_h, timeout=15)
        assert r_restore.status_code == 200
        # confirm restore
        r_check = requests.post(f"{API}/auth/login", json={"email": ANALYST[0], "password": ANALYST[1]}, timeout=15)
        assert r_check.status_code == 200


# ---------- CSV UPLOAD ----------

CSV_STD = (
    "MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,VesselName,IMO,VesselType\n"
    "999000111,2026-06-10T04:00:00Z,53.6,3.8,10.2,90,92,TESTVESSEL,IMO9999999,Tanker\n"
    "999000111,2026-06-10T04:10:00Z,53.61,3.82,10.5,91,93,TESTVESSEL,IMO9999999,Tanker\n"
    "bad,notatime,53.6,3.8,10.2,90,92,TESTVESSEL,IMO9999999,Tanker\n"
)


class TestCSVUpload:
    def test_csv_preview(self, analyst_h):
        files = {"file": ("test.csv", CSV_STD, "text/csv")}
        r = requests.post(f"{API}/ais/csv/preview", files=files, headers=analyst_h, timeout=30)
        assert r.status_code == 200, r.text
        j = r.json()
        m = j["detected_mapping"]
        assert m.get("mmsi") == "MMSI"
        assert m.get("timestamp") == "BaseDateTime"
        assert m.get("lat") == "LAT"
        assert m.get("lon") == "LON"
        assert m.get("sog_kn") == "SOG"
        assert m.get("cog_deg") == "COG"
        assert m.get("heading_deg") == "Heading"
        assert m.get("vessel_name") == "VesselName"
        assert m.get("imo") == "IMO"
        assert m.get("vessel_type") == "VesselType"
        assert j["row_count"] == 3
        assert j["missing_required"] == []
        assert len(j["sample_errors"]) >= 1

    def test_csv_ingest_and_dedup(self, analyst_h):
        files = {"file": ("t1.csv", CSV_STD, "text/csv")}
        r = requests.post(f"{API}/ais/csv/ingest", files=files, headers=analyst_h, timeout=30)
        assert r.status_code == 201, r.text
        j = r.json()
        assert j["inserted"] == 2
        assert j["row_error_count"] == 1

        # re-ingest → dedup
        files2 = {"file": ("t1.csv", CSV_STD, "text/csv")}
        r2 = requests.post(f"{API}/ais/csv/ingest", files=files2, headers=analyst_h, timeout=30)
        assert r2.status_code == 201
        j2 = r2.json()
        assert j2["inserted"] == 0
        assert j2["duplicates"] == 2

    def test_csv_semicolon_lowercase(self, analyst_h):
        csv = (
            "mmsi;timestamp;latitude;longitude\n"
            "999000222;2026-06-10T05:00:00Z;53.7;3.9\n"
            "999000222;2026-06-10T05:10:00Z;53.71;3.92\n"
        )
        files = {"file": ("t2.csv", csv, "text/csv")}
        r = requests.post(f"{API}/ais/csv/preview", files=files, headers=analyst_h, timeout=30)
        assert r.status_code == 200
        m = r.json()["detected_mapping"]
        assert m.get("mmsi") == "mmsi"
        assert m.get("timestamp") == "timestamp"
        assert m.get("lat") == "latitude"
        assert m.get("lon") == "longitude"

    def test_csv_missing_required_no_mapping(self, analyst_h):
        # no LAT/latitude column at all
        csv = "MMSI,BaseDateTime,LON\n999000111,2026-06-10T04:00:00Z,3.8\n"
        files = {"file": ("bad.csv", csv, "text/csv")}
        r = requests.post(f"{API}/ais/csv/ingest", files=files, headers=analyst_h, timeout=30)
        assert r.status_code == 400
        assert "missing" in r.text.lower() or "mapping" in r.text.lower()

    def test_csv_empty(self, analyst_h):
        files = {"file": ("empty.csv", "", "text/csv")}
        r = requests.post(f"{API}/ais/csv/preview", files=files, headers=analyst_h, timeout=15)
        assert r.status_code == 400

    def test_csv_unauth(self):
        files = {"file": ("t.csv", CSV_STD, "text/csv")}
        r = requests.post(f"{API}/ais/csv/preview", files=files, timeout=15)
        assert r.status_code == 401


# ---------- JURISDICTIONS ----------

class TestJurisdictions:
    def test_seeded_zones(self, analyst_h):
        r = requests.get(f"{API}/jurisdictions", headers=analyst_h, timeout=15)
        assert r.status_code == 200
        codes = {z["code"] for z in r.json()}
        for c in ["GBR-EEZ", "BEL-EEZ", "NLD-EEZ", "DEU-EEZ", "DNK-EEZ", "NLD-PS-RTM"]:
            assert c in codes, f"missing seeded zone {c}"

    def test_geojson(self, analyst_h):
        r = requests.get(f"{API}/jurisdictions/geojson", headers=analyst_h, timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert j["type"] == "FeatureCollection"
        assert len(j["features"]) >= 6

    def test_seeded_cases_have_jurisdiction(self, analyst_h):
        r = requests.get(f"{API}/cases", headers=analyst_h, timeout=15)
        assert r.status_code == 200
        cases = r.json()
        for cn in ["SPL-20260610-001", "SPL-20260610-002", "SPL-20260610-003"]:
            c = next((x for x in cases if x["case_number"] == cn), None)
            assert c, f"missing seeded case {cn}"
            assert c.get("primary_jurisdiction") and c["primary_jurisdiction"]["code"] == "NLD-EEZ"

    def test_spill_in_rotterdam_gets_ps(self, analyst_h):
        payload = {
            "geometry": {"type": "Polygon", "coordinates": [[[3.95, 51.95], [4.05, 51.95], [4.05, 52.05], [3.95, 52.05], [3.95, 51.95]]]},
            "acquisition_time": "2026-06-10T06:00:00Z",
            "source": "test", "detection_confidence": 0.7, "estimated_area_km2": 1.0,
        }
        r = requests.post(f"{API}/spill-observations", json=payload, headers=analyst_h, timeout=30)
        assert r.status_code == 201, r.text
        case = r.json()["case"]
        assert case.get("primary_jurisdiction"), "primary_jurisdiction missing"
        assert case["primary_jurisdiction"]["code"] == "NLD-PS-RTM"
        codes = [z["code"] for z in case.get("jurisdictions", [])]
        assert "NLD-PS-RTM" in codes and "NLD-EEZ" in codes

    def test_spill_far_outside(self, analyst_h):
        payload = {
            "geometry": {"type": "Polygon", "coordinates": [[[-10.1, 39.9], [-9.9, 39.9], [-9.9, 40.1], [-10.1, 40.1], [-10.1, 39.9]]]},
            "acquisition_time": "2026-06-10T06:00:00Z",
            "source": "test", "detection_confidence": 0.7, "estimated_area_km2": 1.0,
        }
        r = requests.post(f"{API}/spill-observations", json=payload, headers=analyst_h, timeout=30)
        assert r.status_code == 201, r.text
        case = r.json()["case"]
        assert case.get("primary_jurisdiction") is None
        assert case.get("jurisdictions", []) == []

    def test_admin_zone_crud(self, admin_h, analyst_h):
        # analyst forbidden
        r_a = requests.post(f"{API}/jurisdictions", json={"code": "TEST-Z", "name": "T", "authority": "T", "zone_type": "custom",
                                                          "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}},
                            headers=analyst_h, timeout=15)
        assert r_a.status_code == 403

        code = f"TEST-Z-{int(time.time())}"
        create = {"code": code, "name": "Test Zone", "authority": "Test Authority", "zone_type": "custom",
                  "geometry": {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]}}
        r = requests.post(f"{API}/jurisdictions", json=create, headers=admin_h, timeout=15)
        assert r.status_code == 201, r.text
        zid = r.json()["id"]

        # duplicate
        r_dup = requests.post(f"{API}/jurisdictions", json=create, headers=admin_h, timeout=15)
        assert r_dup.status_code == 400

        # invalid zone_type
        bad = dict(create); bad["code"] = code + "X"; bad["zone_type"] = "bogus"
        r_bad_zt = requests.post(f"{API}/jurisdictions", json=bad, headers=admin_h, timeout=15)
        assert r_bad_zt.status_code == 400

        # invalid geometry
        badg = dict(create); badg["code"] = code + "Y"; badg["geometry"] = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]}
        r_bad_g = requests.post(f"{API}/jurisdictions", json=badg, headers=admin_h, timeout=15)
        assert r_bad_g.status_code == 400

        # update
        r_up = requests.put(f"{API}/jurisdictions/{zid}", json={"active": False}, headers=admin_h, timeout=15)
        assert r_up.status_code == 200
        assert r_up.json()["active"] is False

        # delete
        r_del = requests.delete(f"{API}/jurisdictions/{zid}", headers=admin_h, timeout=15)
        assert r_del.status_code == 200

    def test_resolve_all(self, admin_h):
        r = requests.post(f"{API}/jurisdictions/resolve-all", headers=admin_h, timeout=60)
        assert r.status_code == 200
        j = r.json()
        assert "cases" in j and "by_primary" in j

    def test_resolve_case(self, analyst_h):
        cases = requests.get(f"{API}/cases", headers=analyst_h, timeout=15).json()
        cid = next(c["id"] for c in cases if c["case_number"] == "SPL-20260610-001")
        r = requests.post(f"{API}/cases/{cid}/jurisdiction/resolve", headers=analyst_h, timeout=30)
        assert r.status_code == 200
        j = r.json()
        assert j["primary_jurisdiction"]["code"] == "NLD-EEZ"

    def test_evidence_pdf_still_ok(self, analyst_h):
        cases = requests.get(f"{API}/cases", headers=analyst_h, timeout=15).json()
        cid = next(c["id"] for c in cases if c["case_number"] == "SPL-20260610-001")
        r = requests.get(f"{API}/cases/{cid}/evidence.pdf", headers=analyst_h, timeout=60)
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"


# ---------- VESSEL PROFILE ----------

class TestVesselProfile:
    def test_profile_known(self, analyst_h):
        r = requests.get(f"{API}/vessels/244123456/profile", headers=analyst_h, timeout=30)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["vessel_name"] == "NORDIC TRADER"
        assert j["imo"] == "9483210"
        assert j["summary"]["appearances"] >= 1
        assert j["summary"]["top_ranked"] >= 1
        assert "confirmed" in j["summary"] and "rejected" in j["summary"]
        assert isinstance(j["appearances"], list) and len(j["appearances"]) >= 1
        a = j["appearances"][0]
        for k in ("case_number", "rank", "score", "candidate_status", "case_attribution_status",
                  "confirmed_this_vessel", "primary_jurisdiction", "distance_km", "time_gap_hours"):
            assert k in a, f"missing {k} in appearance"
        s = j["ais_summary"]
        for k in ("fixes", "first_seen", "last_seen", "sources", "quality_flags", "gaps_over_2h", "last_position"):
            assert k in s
        assert "decisions" in j

    def test_profile_decisions_after_review(self, analyst_h):
        # Post a review naming vessel 244123456
        cases = requests.get(f"{API}/cases", headers=analyst_h, timeout=15).json()
        c = next(c for c in cases if c["case_number"] == "SPL-20260610-002")
        review = {"decision": "needs_more_data", "vessel_mmsi": "244123456", "reason_codes": [], "notes": "iter3 test review"}
        r_rev = requests.post(f"{API}/cases/{c['id']}/review", json=review, headers=analyst_h, timeout=15)
        assert r_rev.status_code in (200, 201), r_rev.text

        r = requests.get(f"{API}/vessels/244123456/profile", headers=analyst_h, timeout=30)
        assert r.status_code == 200
        decisions = r.json()["decisions"]
        assert any(d.get("case_number") == "SPL-20260610-002" for d in decisions)

    def test_profile_unknown(self, analyst_h):
        r = requests.get(f"{API}/vessels/000000000/profile", headers=analyst_h, timeout=15)
        assert r.status_code == 404

    def test_profile_unauth(self):
        r = requests.get(f"{API}/vessels/244123456/profile", timeout=15)
        assert r.status_code == 401
