"""Iteration 31 — Public access model: guest, signup (viewer), role-request, admin approval.

Endpoints tested:
- POST /api/auth/guest
- POST /api/auth/signup (weak/no-number/duplicate/role-forcing/disabled)
- GET /api/auth/me
- POST /api/role-requests (viewer only, admin never self-requestable)
- GET /api/role-requests/me
- GET /api/role-requests (admin)
- POST /api/role-requests/{id}/approve, /reject (admin)
- Guest read 200, guest write 403, guest /api/users 403, guest /api/audit 403
- Viewer read 200, viewer write 403
- Admin preserved after everything

Cleanup: created viewer test users deleted at end via admin DELETE /api/users/{id}
and pending/approved role_requests removed by admin approve/reject or user cancel.
"""

import os
import time
import uuid

import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")
load_dotenv(os.path.join(os.path.dirname(__file__), ".env.test"))

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_EMAIL = "shawpriyanshu950@gmail.com"
ADMIN_PW = os.environ.get("TEST_ADMIN_PASSWORD", "Admin#2026")

TS = int(time.time())


def _login(email, pw):
    r = requests.post(f"{BASE}/api/auth/login", json={"email": email, "password": pw}, timeout=15)
    return r


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def admin_tok():
    r = _login(ADMIN_EMAIL, ADMIN_PW)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def created_user_ids():
    """Registry of user ids to clean up at the end."""
    ids = []
    yield ids
    # Cleanup
    try:
        r = _login(ADMIN_EMAIL, ADMIN_PW)
        if r.status_code == 200:
            tok = r.json()["access_token"]
            for uid in ids:
                try:
                    requests.delete(f"{BASE}/api/users/{uid}", headers=_h(tok), timeout=15)
                except Exception:
                    pass
    except Exception:
        pass


# =========================== GUEST ===========================
class TestGuest:
    def test_guest_session_create(self):
        r = requests.post(f"{BASE}/api/auth/guest", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("token_type") == "bearer"
        assert d.get("access_token")
        u = d.get("user", {})
        assert u.get("role") == "guest"
        assert u.get("is_guest") is True
        assert u.get("active") is True

    def test_guest_me(self):
        tok = requests.post(f"{BASE}/api/auth/guest", timeout=15).json()["access_token"]
        r = requests.get(f"{BASE}/api/auth/me", headers=_h(tok), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json().get("role") == "guest"

    def test_guest_can_read_dashboard(self):
        tok = requests.post(f"{BASE}/api/auth/guest", timeout=15).json()["access_token"]
        r = requests.get(f"{BASE}/api/dashboard/summary", headers=_h(tok), timeout=20)
        assert r.status_code == 200, (r.status_code, r.text[:200])

    def test_guest_can_read_cases(self):
        tok = requests.post(f"{BASE}/api/auth/guest", timeout=15).json()["access_token"]
        r = requests.get(f"{BASE}/api/cases?limit=5", headers=_h(tok), timeout=20)
        assert r.status_code == 200, (r.status_code, r.text[:200])

    def test_guest_write_forbidden(self):
        tok = requests.post(f"{BASE}/api/auth/guest", timeout=15).json()["access_token"]
        # Try a write endpoint: analyze-eligible
        r = requests.post(f"{BASE}/api/cases/analyze-eligible", headers=_h(tok), timeout=15)
        assert r.status_code == 403, (r.status_code, r.text[:200])

    def test_guest_users_forbidden(self):
        tok = requests.post(f"{BASE}/api/auth/guest", timeout=15).json()["access_token"]
        r = requests.get(f"{BASE}/api/users", headers=_h(tok), timeout=15)
        assert r.status_code == 403, r.status_code

    def test_guest_audit_forbidden(self):
        tok = requests.post(f"{BASE}/api/auth/guest", timeout=15).json()["access_token"]
        r = requests.get(f"{BASE}/api/audit", headers=_h(tok), timeout=15)
        assert r.status_code == 403, r.status_code

    def test_guest_role_request_forbidden(self):
        tok = requests.post(f"{BASE}/api/auth/guest", timeout=15).json()["access_token"]
        r = requests.post(
            f"{BASE}/api/role-requests",
            json={"requested_role": "analyst"},
            headers=_h(tok),
            timeout=15,
        )
        assert r.status_code in (401, 403), r.status_code


# =========================== SIGNUP ===========================
class TestSignup:
    def test_signup_weak_password_no_letters(self):
        email = f"qa_weak_{TS}_{uuid.uuid4().hex[:6]}@example.com"
        r = requests.post(
            f"{BASE}/api/auth/signup",
            json={"name": "Weak", "email": email, "password": "1234567890"},
            timeout=15,
        )
        assert r.status_code in (400, 422), (r.status_code, r.text)

    def test_signup_weak_password_no_numbers(self):
        email = f"qa_nonum_{TS}_{uuid.uuid4().hex[:6]}@example.com"
        r = requests.post(
            f"{BASE}/api/auth/signup",
            json={"name": "Weak", "email": email, "password": "abcdefghij"},
            timeout=15,
        )
        assert r.status_code in (400, 422), (r.status_code, r.text)

    def test_signup_too_short(self):
        email = f"qa_short_{TS}_{uuid.uuid4().hex[:6]}@example.com"
        r = requests.post(
            f"{BASE}/api/auth/signup",
            json={"name": "Short", "email": email, "password": "short1"},
            timeout=15,
        )
        assert r.status_code in (400, 422), (r.status_code, r.text)

    def test_signup_success_forces_viewer(self, created_user_ids):
        email = f"qa_viewer_{TS}_{uuid.uuid4().hex[:8]}@example.com"
        # Even if client tries to pass role=admin, server MUST force viewer.
        r = requests.post(
            f"{BASE}/api/auth/signup",
            json={
                "name": "QA Viewer",
                "email": email,
                "password": "QaPass1234",
                "role": "admin",  # client-provided, should be ignored
            },
            timeout=15,
        )
        assert r.status_code == 201, r.text
        d = r.json()
        tok = d.get("access_token")
        assert tok
        user = d.get("user", {})
        assert user.get("role") == "viewer", user
        assert user.get("active") is True
        uid = user.get("id")
        assert uid
        created_user_ids.append(uid)

        # Verify /auth/me confirms role
        me = requests.get(f"{BASE}/api/auth/me", headers=_h(tok), timeout=15).json()
        assert me.get("role") == "viewer", me

    def test_signup_duplicate_blocked(self, created_user_ids):
        email = f"qa_dup_{TS}_{uuid.uuid4().hex[:8]}@example.com"
        r1 = requests.post(
            f"{BASE}/api/auth/signup",
            json={"name": "Dup", "email": email, "password": "QaPass1234"},
            timeout=15,
        )
        assert r1.status_code == 201, r1.text
        created_user_ids.append(r1.json()["user"]["id"])
        r2 = requests.post(
            f"{BASE}/api/auth/signup",
            json={"name": "Dup2", "email": email, "password": "QaPass1234"},
            timeout=15,
        )
        assert r2.status_code == 400, (r2.status_code, r2.text)


# =========================== VIEWER RBAC ===========================
@pytest.fixture(scope="module")
def viewer_ctx(created_user_ids):
    email = f"qa_rbac_{TS}_{uuid.uuid4().hex[:8]}@example.com"
    pw = "QaPass1234"
    r = requests.post(
        f"{BASE}/api/auth/signup",
        json={"name": "QA RBAC", "email": email, "password": pw},
        timeout=15,
    )
    assert r.status_code == 201, r.text
    d = r.json()
    created_user_ids.append(d["user"]["id"])
    return {"email": email, "password": pw, "token": d["access_token"], "id": d["user"]["id"]}


class TestViewerRBAC:
    def test_viewer_role_is_viewer(self, viewer_ctx):
        r = requests.get(f"{BASE}/api/auth/me", headers=_h(viewer_ctx["token"]), timeout=15)
        assert r.status_code == 200
        assert r.json().get("role") == "viewer"

    def test_viewer_can_read_dashboard(self, viewer_ctx):
        r = requests.get(
            f"{BASE}/api/dashboard/summary", headers=_h(viewer_ctx["token"]), timeout=20
        )
        assert r.status_code == 200

    def test_viewer_can_read_cases(self, viewer_ctx):
        r = requests.get(
            f"{BASE}/api/cases?limit=5", headers=_h(viewer_ctx["token"]), timeout=20
        )
        assert r.status_code == 200

    def test_viewer_write_forbidden(self, viewer_ctx):
        r = requests.post(
            f"{BASE}/api/cases/analyze-eligible", headers=_h(viewer_ctx["token"]), timeout=15
        )
        assert r.status_code == 403, r.status_code

    def test_viewer_users_forbidden(self, viewer_ctx):
        r = requests.get(f"{BASE}/api/users", headers=_h(viewer_ctx["token"]), timeout=15)
        assert r.status_code == 403, r.status_code


# =========================== ROLE REQUEST ===========================
class TestRoleRequest:
    def test_admin_role_not_requestable(self, viewer_ctx):
        r = requests.post(
            f"{BASE}/api/role-requests",
            json={"requested_role": "admin"},
            headers=_h(viewer_ctx["token"]),
            timeout=15,
        )
        assert r.status_code in (400, 422), (r.status_code, r.text)

    def test_viewer_role_not_requestable(self, viewer_ctx):
        r = requests.post(
            f"{BASE}/api/role-requests",
            json={"requested_role": "viewer"},
            headers=_h(viewer_ctx["token"]),
            timeout=15,
        )
        assert r.status_code in (400, 422), (r.status_code, r.text)

    def test_submit_analyst_request_and_admin_approves(self, admin_tok, created_user_ids):
        # Create a fresh viewer for a clean flow
        email = f"qa_promote_{TS}_{uuid.uuid4().hex[:8]}@example.com"
        pw = "QaPass1234"
        sr = requests.post(
            f"{BASE}/api/auth/signup",
            json={"name": "QA Promote", "email": email, "password": pw},
            timeout=15,
        )
        assert sr.status_code == 201, sr.text
        v = sr.json()
        vtok = v["access_token"]
        vid = v["user"]["id"]
        created_user_ids.append(vid)

        # Submit role request
        rr = requests.post(
            f"{BASE}/api/role-requests",
            json={"requested_role": "analyst", "organization": "QA", "reason": "test"},
            headers=_h(vtok),
            timeout=15,
        )
        assert rr.status_code == 201, rr.text
        req = rr.json()
        req_id = req.get("id")
        assert req_id
        assert req.get("requested_role") == "analyst"
        assert req.get("status") == "pending"

        # Duplicate pending blocked
        dup = requests.post(
            f"{BASE}/api/role-requests",
            json={"requested_role": "supervisor"},
            headers=_h(vtok),
            timeout=15,
        )
        assert dup.status_code == 400, (dup.status_code, dup.text)

        # GET /me shows the pending request
        me_rr = requests.get(
            f"{BASE}/api/role-requests/me", headers=_h(vtok), timeout=15
        )
        assert me_rr.status_code == 200, me_rr.text
        me_body = me_rr.json()
        # endpoint may return {"request": {...}} or the request itself
        me_req = me_body.get("request", me_body)
        assert me_req.get("status") == "pending", me_body
        assert me_req.get("requested_role") == "analyst", me_body

        # Admin lists pending
        ap = requests.get(
            f"{BASE}/api/role-requests?status=pending", headers=_h(admin_tok), timeout=15
        )
        assert ap.status_code == 200, ap.text
        pending_list = ap.json()
        assert any(x.get("id") == req_id for x in pending_list)

        # Non-admin approve forbidden
        forb = requests.post(
            f"{BASE}/api/role-requests/{req_id}/approve", headers=_h(vtok), timeout=15
        )
        assert forb.status_code == 403, forb.status_code

        # Admin approves
        ok = requests.post(
            f"{BASE}/api/role-requests/{req_id}/approve", headers=_h(admin_tok), timeout=15
        )
        assert ok.status_code == 200, ok.text

        # Viewer's stored role now analyst — verify via /auth/me (fresh read from DB per docs)
        me = requests.get(f"{BASE}/api/auth/me", headers=_h(vtok), timeout=15).json()
        assert me.get("role") == "analyst", me

    def test_reject_flow(self, admin_tok, created_user_ids):
        email = f"qa_reject_{TS}_{uuid.uuid4().hex[:8]}@example.com"
        pw = "QaPass1234"
        sr = requests.post(
            f"{BASE}/api/auth/signup",
            json={"name": "QA Reject", "email": email, "password": pw},
            timeout=15,
        )
        assert sr.status_code == 201, sr.text
        v = sr.json()
        created_user_ids.append(v["user"]["id"])
        vtok = v["access_token"]

        rr = requests.post(
            f"{BASE}/api/role-requests",
            json={"requested_role": "supervisor"},
            headers=_h(vtok),
            timeout=15,
        )
        assert rr.status_code == 201, rr.text
        req_id = rr.json()["id"]

        rej = requests.post(
            f"{BASE}/api/role-requests/{req_id}/reject", headers=_h(admin_tok), timeout=15
        )
        assert rej.status_code == 200, rej.text

        me = requests.get(f"{BASE}/api/auth/me", headers=_h(vtok), timeout=15).json()
        assert me.get("role") == "viewer", me


# =========================== ADMIN PRESERVED ===========================
class TestAdminPreserved:
    def test_admin_still_admin(self, admin_tok):
        r = requests.get(f"{BASE}/api/auth/me", headers=_h(admin_tok), timeout=15)
        assert r.status_code == 200
        assert r.json().get("role") == "admin"

    def test_admin_users_access(self, admin_tok):
        r = requests.get(f"{BASE}/api/users", headers=_h(admin_tok), timeout=15)
        assert r.status_code == 200


# =========================== DISABLED USER ===========================
class TestDisabled:
    def test_disabled_login_and_signup_blocked(self, admin_tok, created_user_ids):
        email = f"qa_disabled_{TS}_{uuid.uuid4().hex[:8]}@example.com"
        pw = "QaPass1234"
        sr = requests.post(
            f"{BASE}/api/auth/signup",
            json={"name": "QA Disabled", "email": email, "password": pw},
            timeout=15,
        )
        assert sr.status_code == 201, sr.text
        uid = sr.json()["user"]["id"]
        created_user_ids.append(uid)

        # Disable via admin PATCH
        p = requests.patch(
            f"{BASE}/api/users/{uid}",
            json={"active": False},
            headers=_h(admin_tok),
            timeout=15,
        )
        assert p.status_code == 200, p.text

        # Login should now be blocked
        rlog = _login(email, pw)
        assert rlog.status_code == 403, (rlog.status_code, rlog.text)

        # Re-signup with same email should be blocked (disabled account)
        rre = requests.post(
            f"{BASE}/api/auth/signup",
            json={"name": "QA Disabled Retry", "email": email, "password": pw},
            timeout=15,
        )
        assert rre.status_code == 403, (rre.status_code, rre.text)
