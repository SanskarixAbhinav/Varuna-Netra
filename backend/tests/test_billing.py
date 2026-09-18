import pytest
from unittest.mock import AsyncMock

from routers.billing import PLANS, current_entitlement

@pytest.mark.asyncio
async def test_catalog_contains_viewer_pro_institution():
    assert set(PLANS) == {"viewer", "pro", "institution"}
    assert PLANS["viewer"]["amount"] == 0
    assert PLANS["pro"]["price_env"] == "STRIPE_PRICE_PRO"

@pytest.mark.asyncio
async def test_guest_entitlement_is_free_viewer():
    ent = await current_entitlement({"role": "guest"})
    assert ent["plan"] == "viewer"
    assert ent["status"] == "active"
    assert ent["source"] == "guest"
