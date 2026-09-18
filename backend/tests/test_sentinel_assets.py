"""Sentinel asset resolution: quicklook (visualization) vs SAR asset (analysis). Pure unit tests, no network."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sentinel_assets as sa  # noqa: E402

ITEM_FULL = {"id": "S1X", "collection": "sentinel-1-grd", "assets": {
    "vv": {"type": "image/tiff; application=geotiff; profile=cloud-optimized", "roles": ["data"], "href": "https://x.blob.core.windows.net/a/vv.tiff"},
    "vh": {"type": "image/tiff; application=geotiff; profile=cloud-optimized", "roles": ["data"], "href": "https://x.blob.core.windows.net/a/vh.tiff"},
    "thumbnail": {"type": "image/png", "roles": ["thumbnail"], "href": "https://x/thumb.png"},
    "rendered_preview": {"type": "image/png", "roles": ["overview"], "href": "https://pc/preview.png"},
    "safe-manifest": {"type": "application/xml", "roles": ["metadata"], "href": "https://x/m.xml"}}}
ITEM_NO_THUMB = {"id": "S1Y", "collection": "sentinel-1-grd", "assets": {
    "hh": {"type": "image/tiff; application=geotiff; profile=cloud-optimized", "roles": ["data"], "href": "https://x.blob.core.windows.net/a/hh.tiff"}}}
ITEM_NO_SAR = {"id": "S1Z", "collection": "sentinel-1-grd", "assets": {"thumbnail": {"type": "image/png", "roles": ["thumbnail"], "href": "https://x/t.png"}}}


def test_asset_inventory_is_inspected_not_assumed():
    inv = sa.summarize_assets(ITEM_FULL)
    assert inv["sar_assets"] == ["vv", "vh"] and inv["analysis_asset"] == "vv"
    assert inv["native_preview_asset"] == "rendered_preview" and inv["preview_href"] == "https://pc/preview.png"
    assert "safe-manifest" in inv["available_assets"]


def test_sar_without_thumbnail_is_still_analyzable():
    inv = sa.summarize_assets(ITEM_NO_THUMB)
    assert inv["analysis_asset"] == "hh" and inv["native_preview_asset"] is None and inv["preview_href"] is None


def test_no_sar_asset_detected():
    inv = sa.summarize_assets(ITEM_NO_SAR)
    assert inv["sar_assets"] == [] and inv["analysis_asset"] is None


def test_case_summary_states():
    case = {"scene_id": None}
    s = sa.scene_status_summary(None, case)
    assert s["state"] == "NO_SCENE_SELECTED" and "No Sentinel-1 scene" in s["reason"]
    manual = {"id": "m", "provider_scene_id": "MANUAL", "acquisition_time": "2026-01-01T00:00:00Z", "metadata": {}}
    s = sa.scene_status_summary(manual, {"scene_id": "m"})
    assert s["state"] == "SAR_ASSET_UNAVAILABLE" and s["sar_available"] is False and "SAR asset missing" in s["reason"]
    stac_no_thumb = {"id": "r", "provider_scene_id": "S1Y", "acquisition_time": "2026-01-01T00:00:00Z", "metadata": {"stac_collection": "sentinel-1-grd"}, "assets": sa.summarize_assets(ITEM_NO_THUMB)}
    s = sa.scene_status_summary(stac_no_thumb, {"scene_id": "r"})
    assert s["state"] == "SAR_READY" and s["quicklook_available"] is False and s["analysis_asset"] == "hh"
    generating = {**stac_no_thumb, "quicklook_status": "generating"}
    assert sa.scene_status_summary(generating, {"scene_id": "r"})["state"] == "QUICKLOOK_GENERATING"


def test_signed_href_only_for_blob_and_never_persisted(monkeypatch):
    async def fake_token(col):
        return "st=1&sig=abc"
    monkeypatch.setattr(sa, "sas_token", fake_token)
    signed = asyncio.run(sa.signed_href("sentinel-1-grd", "https://x.blob.core.windows.net/a/vv.tiff"))
    assert signed.endswith("?st=1&sig=abc")
    assert asyncio.run(sa.signed_href("sentinel-1-grd", "https://pc/preview.png")) == "https://pc/preview.png"


def test_expired_token_regenerated(monkeypatch):
    calls = {"n": 0}

    class R:
        def __init__(self, code):
            self.status_code = code

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def head(self, url):
            calls["n"] += 1
            return R(403 if calls["n"] == 1 else 200)

    async def fake_token(col):
        return f"tok{calls['n']}"
    monkeypatch.setattr(sa, "sas_token", fake_token)
    monkeypatch.setattr(sa.httpx, "AsyncClient", C)
    ok, err = asyncio.run(sa.sar_asset_accessible("sentinel-1-grd", "https://x.blob.core.windows.net/a/vv.tiff"))
    assert ok and err is None and calls["n"] == 2


def test_missing_sar_raises_specific_error():
    err = sa.SceneAssetError("sar_missing", "SAR asset missing — x")
    assert err.code == "sar_missing" and "SAR asset missing" in str(err)
