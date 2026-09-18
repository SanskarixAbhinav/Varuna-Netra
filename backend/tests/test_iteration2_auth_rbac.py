"""SentinelMar Iteration 2 tests: auth, RBAC, weather, PDF, audit identity."""
import os
import time
import uuid
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE = line.split("=", 1)[1].strip()
API = f"{BASE}/api"

ADMIN = ("shawpriyanshu950@gmail.com", os.environ["TEST_ADMIN_PASSWORD"])
SUPERVISOR = ("supervisor@sentinelmar.demo", os.environ["TEST_SUPERVISOR_PASSWORD"])
ANALYST = ("analyst@sentinelmar.demo", os.environ["TEST_ANALYST_PASSWORD"])


def _login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password})
    return r


def _token(email, password):
    r = _login(email, password)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def admin_tok():
    return _token(*ADMIN)


@pytest.fixture(scope="module")
def sup_tok():
    return _token(*SUPERVISOR)


@pytest.fixture(scope="module")
def an_tok():
    return _token(*ANALYST)


# ----- AUTH BACKEND -----
def test_unauth_stats_401():
    for path in ["/stats", "/cases", "/jobs"]:
        r = requests.get(f"{API}{path}")
        assert r.status_code == 401, f"{path}: {r.status_code}"


def test_login_all_roles():
    for e, p in [ADMIN, SUPERVISOR, ANALYST]:
        r = _login(e, p)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "access_token" in d
        assert d["user"]["email"] == e.lower()
        assert d["user"]["role"] in ("admin", "supervisor", "analyst")


def test_login_wrong_password():
    r = _login(ADMIN[0], "WRONG_PW_xxx")
    assert r.status_code == 401


def test_login_lockout_fake_email():
    # Use non-existent email to avoid locking demo accounts
    fake = f"nonexistent_{uuid.uuid4().hex[:8]}@example.test"
    # Try up to 15 attempts (behind K8s ingress, request.client.host may vary → identifier tracking may split)
    statuses = []
    for _ in range(15):
        statuses.append(_login(fake, "badpass").status_code)
        if statuses[-1] == 429:
            break
    assert 429 in statuses, f"Never got 429; statuses={statuses}"


def test_auth_me_and_logout(an_tok):
    r = requests.get(f"{API}/auth/me", headers=_hdr(an_tok))
    assert r.status_code == 200
    assert r.json()["email"] == ANALYST[0]
    r = requests.post(f"{API}/auth/logout", headers=_hdr(an_tok))
    assert r.status_code == 200


# ----- RBAC BACKEND -----
def test_alert_ack_rbac(an_tok, sup_tok):
    alerts = requests.get(f"{API}/alerts", headers=_hdr(an_tok)).json()
    unack = next((a for a in alerts if not a.get("acknowledged")), None)
    if not unack:
        pytest.skip("no unack alerts")
    r = requests.post(f"{API}/alerts/{unack['id']}/ack", headers=_hdr(an_tok))
    assert r.status_code == 403
    r = requests.post(f"{API}/alerts/{unack['id']}/ack", headers=_hdr(sup_tok))
    assert r.status_code == 200
    assert r.json().get("acknowledged_by") == SUPERVISOR[0]


def _cases(tok):
    return requests.get(f"{API}/cases", headers=_hdr(tok)).json()


def _get_case_num(tok, num):
    return next((c for c in _cases(tok) if c["case_number"] == num), None)


def test_override_rbac_and_flow(an_tok, sup_tok):
    # create a fresh spill+case for destructive override
    scenes = requests.get(f"{API}/scenes", headers=_hdr(an_tok)).json()
    sid = scenes[0]["id"]
    poly = {"type": "Polygon", "coordinates": [[[3.61, 53.45], [3.62, 53.45], [3.62, 53.46], [3.61, 53.46], [3.61, 53.45]]]}
    r = requests.post(f"{API}/spill-observations?correlate=true", json={
        "scene_id": sid, "geometry": poly, "acquisition_time": "2026-06-10T05:30:00Z",
        "source": "test", "detection_confidence": 0.8, "quality_flags": [], "processing_version": "t"
    }, headers=_hdr(an_tok))
    assert r.status_code == 201, r.text
    case_id = r.json()["case"]["id"]

    # analyst cannot override
    body = {"attribution_status": "insufficient_evidence", "notes": "closing after inspection", "close_case": True}
    r = requests.post(f"{API}/cases/{case_id}/override", json=body, headers=_hdr(an_tok))
    assert r.status_code == 403

    # supervisor can
    r = requests.post(f"{API}/cases/{case_id}/override", json=body, headers=_hdr(sup_tok))
    assert r.status_code == 201, r.text
    upd = requests.get(f"{API}/cases/{case_id}", headers=_hdr(sup_tok)).json()
    assert upd["attribution_status"] == "insufficient_evidence"
    assert upd["review_state"] == "supervisor_override"
    assert upd["status"] == "closed"
    reviews = requests.get(f"{API}/cases/{case_id}/reviews", headers=_hdr(sup_tok)).json()
    assert any(rv["decision"] == "supervisor_override" for rv in reviews)


def test_users_rbac(an_tok, sup_tok, admin_tok):
    assert requests.get(f"{API}/users", headers=_hdr(an_tok)).status_code == 403
    assert requests.get(f"{API}/users", headers=_hdr(sup_tok)).status_code == 403
    r = requests.get(f"{API}/users", headers=_hdr(admin_tok))
    assert r.status_code == 200
    assert len(r.json()) >= 3


def test_admin_user_crud(admin_tok):
    email = f"test_{uuid.uuid4().hex[:8]}@example.com"
    # short password
    r = requests.post(f"{API}/users", json={"email": email, "name": "T", "role": "analyst", "password": "short"}, headers=_hdr(admin_tok))
    assert r.status_code in (400, 422)
    # invalid role
    r = requests.post(f"{API}/users", json={"email": email, "name": "T", "role": "badrole", "password": "longenough"}, headers=_hdr(admin_tok))
    assert r.status_code == 400
    # create ok
    r = requests.post(f"{API}/users", json={"email": email, "name": "TestUser", "role": "analyst", "password": "Password#123"}, headers=_hdr(admin_tok))
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    # duplicate email
    r = requests.post(f"{API}/users", json={"email": email, "name": "X", "role": "analyst", "password": "Password#123"}, headers=_hdr(admin_tok))
    assert r.status_code == 400
    # can log in
    r = _login(email, "Password#123")
    assert r.status_code == 200
    # patch role
    r = requests.patch(f"{API}/users/{uid}", json={"role": "supervisor"}, headers=_hdr(admin_tok))
    assert r.status_code == 200 and r.json()["role"] == "supervisor"
    # active toggle
    r = requests.patch(f"{API}/users/{uid}", json={"active": False}, headers=_hdr(admin_tok))
    assert r.status_code == 200 and r.json()["active"] is False
    # self-deactivate blocked
    me = requests.get(f"{API}/auth/me", headers=_hdr(admin_tok)).json()
    r = requests.patch(f"{API}/users/{me['id']}", json={"active": False}, headers=_hdr(admin_tok))
    assert r.status_code == 400
    r = requests.patch(f"{API}/users/{me['id']}", json={"role": "analyst"}, headers=_hdr(admin_tok))
    assert r.status_code == 400
    r = requests.delete(f"{API}/users/{me['id']}", headers=_hdr(admin_tok))
    assert r.status_code == 400
    # delete created
    r = requests.delete(f"{API}/users/{uid}", headers=_hdr(admin_tok))
    assert r.status_code == 200


def test_seed_admin_only(an_tok, sup_tok, admin_tok):
    assert requests.post(f"{API}/seed", headers=_hdr(an_tok)).status_code == 403
    assert requests.post(f"{API}/seed", headers=_hdr(sup_tok)).status_code == 403
    # skip actual admin re-seed to preserve state; just check permitted status != 403
    # Not calling admin seed to preserve data


# ----- IDENTITY IN AUDIT -----
def test_review_identity_in_audit(an_tok):
    # Use a fresh case to avoid touching demo -001
    scenes = requests.get(f"{API}/scenes", headers=_hdr(an_tok)).json()
    sid = scenes[0]["id"]
    poly = {"type": "Polygon", "coordinates": [[[3.65, 53.5], [3.66, 53.5], [3.66, 53.51], [3.65, 53.51], [3.65, 53.5]]]}
    r = requests.post(f"{API}/spill-observations?correlate=true", json={
        "scene_id": sid, "geometry": poly, "acquisition_time": "2026-06-10T05:30:00Z",
        "source": "test", "detection_confidence": 0.7, "quality_flags": [], "processing_version": "t"
    }, headers=_hdr(an_tok))
    assert r.status_code == 201
    case_id = r.json()["case"]["id"]

    # correlate sync -> job actor + audit actor should be analyst email
    r = requests.post(f"{API}/cases/{case_id}/correlate", json={"sync": True}, headers=_hdr(an_tok))
    assert r.status_code in (200, 202)
    job = r.json()
    assert job.get("actor") == ANALYST[0]

    # review needs_more_data
    r = requests.post(f"{API}/cases/{case_id}/review", json={
        "decision": "needs_more_data", "reason_codes": ["RC01_TRACK_OVERLAP"], "notes": "need more"
    }, headers=_hdr(an_tok))
    assert r.status_code in (200, 201), r.text
    reviews = requests.get(f"{API}/cases/{case_id}/reviews", headers=_hdr(an_tok)).json()
    assert reviews[-1]["analyst"] == ANALYST[0]
    assert reviews[-1]["analyst_role"] == "analyst"

    audit = requests.get(f"{API}/audit", params={"entity_id": case_id}, headers=_hdr(an_tok)).json()
    kinds = [a["action"] for a in audit]
    assert any("review.needs_more_data" in k for k in kinds)
    for a in audit:
        assert a.get("actor") == ANALYST[0] or a.get("actor") is None  # some system events may lack actor


# ----- LIVE WEATHER -----
def test_live_weather_fetch(an_tok):
    case = _get_case_num(an_tok, "SPL-20260610-003")
    if not case:
        pytest.skip("SPL-20260610-003 not seeded")
    r = requests.post(f"{API}/cases/{case['id']}/environment/fetch", headers=_hdr(an_tok))
    assert r.status_code == 200, r.text
    d = r.json()
    env = d["environment"]
    if env.get("source") != "open-meteo" or not d.get("wind") or not d.get("current"):
        pytest.skip(f"Open-Meteo unreachable / degraded: {env}")
    assert d["wind"].get("speed_ms") is not None
    assert d["current"] is not None
    # Verify persisted
    full = requests.get(f"{API}/cases/{case['id']}", headers=_hdr(an_tok)).json()
    spill = full["spill_observation"]
    assert spill.get("wind") is not None
    assert spill.get("current") is not None
    # Correlate sync
    r = requests.post(f"{API}/cases/{case['id']}/correlate", json={"sync": True}, headers=_hdr(an_tok))
    assert r.status_code in (200, 202)


def test_correlate_with_fetch_env(an_tok):
    # Create fresh case
    scenes = requests.get(f"{API}/scenes", headers=_hdr(an_tok)).json()
    sid = scenes[0]["id"]
    poly = {"type": "Polygon", "coordinates": [[[3.71, 53.55], [3.72, 53.55], [3.72, 53.56], [3.71, 53.56], [3.71, 53.55]]]}
    r = requests.post(f"{API}/spill-observations?correlate=true", json={
        "scene_id": sid, "geometry": poly, "acquisition_time": "2026-06-10T05:30:00Z",
        "source": "test", "detection_confidence": 0.6, "quality_flags": [], "processing_version": "t"
    }, headers=_hdr(an_tok))
    assert r.status_code == 201
    case_id = r.json()["case"]["id"]
    r = requests.post(f"{API}/cases/{case_id}/correlate", json={"sync": True, "fetch_environment": True}, headers=_hdr(an_tok))
    assert r.status_code in (200, 202)
    job = r.json()
    log = " ".join(job.get("result", {}).get("processing_log", []) + job.get("log", []) or [])
    # not asserting on log text - external API may fail


# ----- PDF -----
def test_pdf_export(an_tok):
    case = _get_case_num(an_tok, "SPL-20260610-002") or _cases(an_tok)[0]
    # unauth
    r = requests.get(f"{API}/cases/{case['id']}/evidence.pdf")
    assert r.status_code == 401
    r = requests.get(f"{API}/cases/{case['id']}/evidence.pdf", headers={"Authorization": f"Bearer {an_tok}"})
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert "attachment" in r.headers.get("content-disposition", "").lower()
    assert "-evidence.pdf" in r.headers.get("content-disposition", "")
    assert r.content.startswith(b"%PDF")
    assert len(r.content) > 20_000
    # audit
    audit = requests.get(f"{API}/audit", params={"entity_id": case["id"]}, headers=_hdr(an_tok)).json()
    exp = [a for a in audit if a["action"] == "evidence.exported"]
    assert exp
    assert exp[0]["actor"] == ANALYST[0]


# ----- REGRESSION -----
def test_regression_ingest_with_auth(an_tok):
    # Scene create
    payload = {
        "provider": "sentinel-1", "provider_scene_id": f"TEST_ITER2_{int(time.time())}",
        "sensor_mode": "IW", "polarization": "VV",
        "acquisition_time": "2026-06-14T00:00:00Z",
        "footprint": {"type": "Polygon", "coordinates": [[[3.0, 53.0], [4.0, 53.0], [4.0, 54.0], [3.0, 54.0], [3.0, 53.0]]]},
        "storage_ref": "s3://test/x.tiff", "metadata": {}
    }
    r = requests.post(f"{API}/scenes", json=payload, headers=_hdr(an_tok))
    assert r.status_code == 201
    # AIS
    r = requests.post(f"{API}/ais/positions", json={"positions": [
        {"mmsi": "222333444", "vessel_name": "TEST_ITER2", "timestamp": "2026-06-14T04:00:00Z",
         "lat": 53.5, "lon": 3.7, "sog_kn": 10, "cog_deg": 90, "source": "test"}
    ]}, headers=_hdr(an_tok))
    assert r.status_code in (200, 201)


def test_geojson_track_props(an_tok):
    case = _get_case_num(an_tok, "SPL-20260610-001")
    gj = requests.get(f"{API}/cases/{case['id']}/geojson", headers=_hdr(an_tok)).json()
    tracks = [f for f in gj["features"] if f["properties"].get("layer") == "track"]
    assert tracks
    p = tracks[0]["properties"]
    assert "timestamps" in p and "sog" in p and "cog" in p
