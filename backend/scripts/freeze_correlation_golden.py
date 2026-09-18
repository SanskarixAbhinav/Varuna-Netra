"""Freeze correlation outputs for the seeded cases into tests/fixtures/correlation_golden.json (run against the trusted engine)."""
import asyncio
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import db, to_utc  # noqa: E402
from models import CorrelationParams  # noqa: E402
from correlation import run_correlation  # noqa: E402
from tests.test_correlation_golden import normalise  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "correlation_golden.json")
PARAM_SETS = [{}, {"fill_gaps": False}, {"corridor_km": 40, "window_hours_before": 36, "use_observation_environment": False}]


async def main():
    cases = await db.cases.find({"case_number": {"$regex": "^SPL-20260610-00[1-3]$"}}, {"_id": 0}).to_list(10)
    out = []
    for case in cases:
        spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0})
        spill["acquisition_time"] = to_utc(spill["acquisition_time"])
        t0 = spill["acquisition_time"]
        lon, lat = spill["centroid"]["coordinates"]
        for ps in PARAM_SETS:
            params = CorrelationParams(**ps)
            radius_km = params.corridor_km + spill.get("extent_km", 0)
            q = {"timestamp": {"$gte": t0 - timedelta(hours=params.window_hours_before), "$lte": t0 + timedelta(hours=params.window_hours_after)},
                 "location": {"$geoWithin": {"$centerSphere": [[lon, lat], radius_km / 6371.0088]}}}
            positions = await db.ais_positions.find(q, {"_id": 0, "location": 0, "dedup_hash": 0}).sort([("timestamp", 1), ("id", 1)]).to_list(50000)
            for p in positions:
                p["timestamp"] = to_utc(p["timestamp"])
            expected = normalise(run_correlation(spill, positions, params))
            out.append({"case_number": case["case_number"], "params": ps, "spill": json.loads(json.dumps(spill, default=str)),
                        "positions": json.loads(json.dumps(positions, default=str)), "expected": expected})
            print(case["case_number"], ps, "->", expected["overall_status"], len(expected["candidates"]), "candidates")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"cases": out}, f, default=str)
    print("wrote", OUT, len(out), "golden cases")


asyncio.run(main())
