import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env.test"))

import inspect
import pytest
import requests

DEMO_MARKERS = ("SPL-20260610-001", "NORDIC TRADER", "244123456")
LEGACY_PREAUTH = ("test_sentinelmar_backend.py",)


def _demo_purged() -> bool:
    base = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
    if not base:
        return False
    try:
        tok = requests.post(f"{base}/api/auth/login", json={"email": "analyst@sentinelmar.demo", "password": os.environ["TEST_ANALYST_PASSWORD"]}, timeout=15).json()["access_token"]
        return bool(requests.get(f"{base}/api/system/data-mode", headers={"Authorization": f"Bearer {tok}"}, timeout=15).json().get("demo_purged"))
    except Exception:  # noqa: BLE001
        return False


def pytest_collection_modifyitems(config, items):
    purged = _demo_purged()
    for item in items:
        fname = os.path.basename(str(item.fspath))
        if fname in LEGACY_PREAUTH:
            item.add_marker(pytest.mark.skip(reason="legacy pre-auth iteration-1 suite (unauthenticated); superseded by role-based suites"))
            continue
        if purged and _needs_demo(item):
            item.add_marker(pytest.mark.skip(reason="depends on the seeded demo dataset (purged — LIVE mode, real Sentinel-1 data only)"))


def _src(obj) -> str:
    try:
        return inspect.getsource(obj)
    except (OSError, TypeError):
        return ""


def _needs_demo(item) -> bool:
    texts = [_src(item.function)]
    if item.cls is not None:
        texts.append(_src(item.cls))
    fm = getattr(item, "_fixtureinfo", None)
    if fm is not None:
        defs = item.session._fixturemanager._arg2fixturedefs
        for name in fm.names_closure:
            for fd in defs.get(name, []):
                texts.append(_src(fd.func))
    return any(m in t for t in texts for m in DEMO_MARKERS)
