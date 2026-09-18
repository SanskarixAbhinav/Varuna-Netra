"""Unit tests for the AISStream worker: parsing, coverage, no demo fallback. Run: python -m pytest tests/test_ais_live.py -q -p no:xdist -o addopts=''"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import ais_live  # noqa: E402

ENV = {"MessageType": "PositionReport", "MetaData": {"MMSI": 419001234, "ShipName": "MV TEST ", "latitude": 18.9, "longitude": 72.8, "time_utc": "2026-09-10 12:00:00.123 +0000 UTC"},
       "Message": {"PositionReport": {"UserID": 419001234, "Latitude": 18.9, "Longitude": 72.8, "Sog": 11.2, "Cog": 245.0, "TrueHeading": 246, "NavigationalStatus": 0}}}


def test_missing_key_no_demo(monkeypatch):
    monkeypatch.delenv("AISSTREAM_API_KEY", raising=False)
    st = ais_live.status()
    assert st["configured"] is False and st["connected"] is False and st["mode"] == "live" and st["reason"] == "API key not configured"
    assert st["vessels_active"] == 0 and st["messages_received"] == ais_live.state["messages"]  # runtime, not fabricated


def test_configured_key_flag(monkeypatch):
    monkeypatch.setenv("AISSTREAM_API_KEY", "x")
    assert ais_live.status()["configured"] is True
    assert "x" not in json.dumps(ais_live.status())


def test_bbox_format_not_swapped():
    boxes = ais_live.to_aisstream_boxes([ais_live.REGIONS["west_coast"]["bbox"]])
    (lat0, lon0), (lat1, lon1) = boxes[0]
    assert -90 <= lat0 < lat1 <= 90 and 60 < lon0 < lon1 < 100  # lat first, Indian longitudes


def test_default_regions_in_indian_waters():
    for k in ais_live.DEFAULT_REGIONS:
        s, w, n, e = ais_live.REGIONS[k]["bbox"]
        assert 5 <= s < n <= 25 and 65 <= w < e <= 96
    for k, r in ais_live.REGIONS.items():
        ais_live.validate_bbox(*r["bbox"])
        assert r.get("global") or k in ais_live.DEFAULT_REGIONS


def test_validate_bbox_rejects_bad():
    with pytest.raises(ValueError):
        ais_live.validate_bbox(10, 70, 5, 80)
    with pytest.raises(ValueError):
        ais_live.validate_bbox(10, 70, 12, 200)


def test_expand_bbox_grows():
    s, w, n, e = ais_live.expand_bbox(18, 72, 19, 73, 100)
    assert s < 18 and w < 72 and n > 19 and e > 73


def test_position_report_parsing():
    p = ais_live.to_position(ENV)
    assert p.mmsi == "419001234" and p.vessel_name == "MV TEST" and p.lat == 18.9 and p.sog_kn == 11.2 and p.heading_deg == 246 and p.source == "AISStream"


def test_class_b_parsing():
    m = {"MessageType": "StandardClassBPositionReport", "MetaData": {"MMSI": 1, "time_utc": ""}, "Message": {"StandardClassBPositionReport": {"UserID": 1, "Latitude": 9.9, "Longitude": 76.2, "Sog": 4.0, "Cog": 90.0, "TrueHeading": 511}}}
    p = ais_live.to_position(m)
    assert p and p.lon == 76.2 and p.heading_deg == 511 and p.cog_deg == 90.0


def test_invalid_coordinates_rejected():
    bad = json.loads(json.dumps(ENV))
    bad["Message"]["PositionReport"]["Latitude"] = 91
    assert ais_live.to_position(bad) is None
    bad["Message"]["PositionReport"].update({"Latitude": 10, "Longitude": 181})
    assert ais_live.to_position(bad) is None


def test_binary_frame_decoding():
    assert ais_live.decode_frame(json.dumps(ENV).encode("utf-8"))["MessageType"] == "PositionReport"
    assert ais_live.decode_frame(b"\xff\xfe not json") is None
    assert ais_live.decode_frame("[1,2]") is None


def test_subscription_confirmation_and_handle():
    ais_live.state.update({"connected": False, "subscription_confirmed": False, "active": {}})
    ais_live._handle({"MessageType": "SubscriptionConfirmation"})
    assert ais_live.state["subscription_confirmed"] is True
    ais_live._handle(ENV)
    assert "419001234" in ais_live.state["active"] and ais_live.state["positions"] >= 1
    with pytest.raises(RuntimeError):
        ais_live._handle({"error": "Api Key Is Not Valid"})


def test_stale_vessels_removed():
    from datetime import datetime, timedelta, timezone
    ais_live.state["active"]["old"] = {"received_at": datetime.now(timezone.utc) - timedelta(minutes=ais_live.STALE_MIN + 1)}
    ais_live._touch_active(ais_live.to_position(ENV), 0)
    assert "old" not in ais_live.state["active"]


def test_single_worker():
    import asyncio

    async def go():
        ais_live.start()
        t1 = ais_live._task
        ais_live.start()
        assert ais_live._task is t1
        t1.cancel()
    asyncio.run(go())
