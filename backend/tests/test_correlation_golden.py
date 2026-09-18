"""Golden-master test for the correlation engine: refactors must reproduce the frozen output byte-for-byte."""
import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from correlation import run_correlation  # noqa: E402
from models import CorrelationParams  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "correlation_golden.json")
_iso = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)  # noqa: E731


def _load():
    with open(FIXTURE) as f:
        return json.load(f)


def normalise(result):
    """Drop wall-clock log timestamps; everything else must match exactly."""
    r = json.loads(json.dumps(result, default=str, sort_keys=True))
    for e in r["processing_log"]:
        e.pop("t", None)
    return r


def run_case(case):
    spill = dict(case["spill"])
    spill["acquisition_time"] = _iso(spill["acquisition_time"])
    positions = [{**p, "timestamp": _iso(p["timestamp"])} for p in case["positions"]]
    return normalise(run_correlation(spill, positions, CorrelationParams(**case["params"])))


@pytest.mark.skipif(not os.path.exists(FIXTURE), reason="golden fixture not generated")
@pytest.mark.parametrize("idx", range(len(_load()["cases"])) if os.path.exists(FIXTURE) else [])
def test_golden_output_identical(idx):
    case = _load()["cases"][idx]
    got = run_case(case)
    assert got == case["expected"], f"correlation output drifted for {case['case_number']}"
