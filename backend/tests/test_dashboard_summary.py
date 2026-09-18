"""Dashboard summary: (1) deterministic 5/2/3 semantics on an ISOLATED throwaway database; (2) live API shape/invariants. Never mutates the app DB."""
import os
import uuid
import asyncio
from datetime import datetime, timezone

import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")


def _case(i, source="sar", status="open", review="pending", demo=False, origin="detector", attribution="indeterminate"):
    d = {"id": str(uuid.uuid4()), "case_number": f"T-{i}", "source": source, "status": status, "review_state": review, "attribution_status": attribution, "origin": origin,
         "created_at": datetime.now(timezone.utc)}
    if demo:
        d["is_demo"] = True
        d["origin"] = "demo"
    return d


def _alert(case_id, ack=False, demo=False):
    d = {"id": str(uuid.uuid4()), "case_id": case_id, "acknowledged": ack, "severity": "medium", "kind": "new_spill", "created_at": datetime.now(timezone.utc)}
    if demo:
        d["is_demo"] = True
    return d


def test_summary_5_real_2_pending_3_alerts_isolated():
    import sys
    sys.path.insert(0, "/app/backend")
    from motor.motor_asyncio import AsyncIOMotorClient
    from dashboard import compute_summary

    async def run():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        name = f"varuna_dashtest_{uuid.uuid4().hex[:8]}"
        tdb = client[name]
        try:
            real = [_case(1), _case(2)]  # 2 pending (open + pending)
            real += [_case(3, status="open", review="confirmed", attribution="analyst_confirmed"), _case(4, status="open", review="reviewed", attribution="probable"), _case(5, status="open", review="needs_more_data")]
            demo = [_case(100 + i, source="demo" if i % 2 else "sar", demo=True) for i in range(20)]
            imported = [_case(200 + i, origin="imported") for i in range(7)]  # API-registered polygons without a scene: excluded too
            await tdb.cases.insert_many(real + demo + imported)
            alerts = [_alert(real[0]["id"]), _alert(real[1]["id"]), _alert(None)]  # 3 real, unread
            alerts += [_alert(real[2]["id"], ack=True)]  # acknowledged real alert: counted in total, not unread
            alerts += [_alert(demo[i]["id"]) for i in range(8)] + [_alert(None, demo=True), _alert(None, demo=True)]  # 10 demo
            alerts += [_alert(imported[0]["id"])]
            await tdb.alerts.insert_many(alerts)
            s = await compute_summary(tdb)
            assert s["live_cases"] == 5 and s["cases"]["total"] == 5, s["cases"]
            assert s["pending_review"] == 2, s["pending"]
            assert s["probable_confirmed"] == 2
            assert s["alerts"]["unread"] == 3 and s["alerts"]["total"] == 4, s["alerts"]
            assert s["demo"]["cases"] == 20 and s["demo"]["imported"] == 7 and s["demo"]["alerts"] == 11
            assert s["cases"]["all_records"] == 32 and s["cases"]["by_origin"] == {"detector": 5, "demo": 20, "imported": 7}
        finally:
            await client.drop_database(name)
    asyncio.run(run())


@pytest.mark.skipif(not BASE or "TEST_ANALYST_PASSWORD" not in os.environ, reason="live API env not available")
def test_live_summary_shape_and_invariants():
    tok = requests.post(f"{BASE}/api/auth/login", json={"email": "analyst@sentinelmar.demo", "password": os.environ["TEST_ANALYST_PASSWORD"]}, timeout=15).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    d = requests.get(f"{BASE}/api/dashboard/summary", headers=h, timeout=20).json()
    assert d["source"] == "database"
    assert d["pending"]["total"] <= d["cases"]["open"] <= d["cases"]["total"]
    assert d["cases"]["open"] + d["cases"]["closed"] == d["cases"]["total"]
    assert d["alerts"]["unread"] <= d["alerts"]["total"] and d["alerts"]["critical"] <= d["alerts"]["unread"]
    assert "semantics" in d and "api_key" not in str(d).lower()
    # /stats derives from the same summary; list endpoints agree with the counters
    st = requests.get(f"{BASE}/api/stats", headers=h, timeout=20).json()
    assert st["cases_total"] == d["cases"]["total"] and st["pending_review"] == d["pending_review"] and st["alerts_unacknowledged"] == d["alerts"]["unread"]
    assert d["live_cases"] == d["cases"]["open"] and d["pending_review"] <= d["live_cases"]
    real_open = requests.get(f"{BASE}/api/cases?origin=real&status=open&limit=1000", headers=h, timeout=20).json()
    assert len(real_open) == d["live_cases"] and all(c["origin"] in ("detector", "analyst") for c in real_open)
    pending = requests.get(f"{BASE}/api/cases?origin=real&status=open&review_state=pending&limit=1000", headers=h, timeout=20).json()
    assert len(pending) == d["pending_review"]
    imported = requests.get(f"{BASE}/api/cases?origin=imported&limit=1000", headers=h, timeout=20).json()
    assert len(imported) == d["demo"]["imported"] and all(c["origin"] == "imported" for c in imported)
    everything = requests.get(f"{BASE}/api/cases?origin=all&limit=1000", headers=h, timeout=20).json()
    assert len(everything) == d["cases"]["all_records"]
    unread = requests.get(f"{BASE}/api/alerts?unacknowledged=true&limit=500", headers=h, timeout=20).json()
    assert len(unread) == d["alerts"]["unread"]
