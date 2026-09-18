import os
from datetime import datetime, timezone
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db, clean, audit

router = APIRouter(prefix="/billing", tags=["billing"])

PLANS = {
    "viewer": {"id": "viewer", "name": "Viewer", "description": "Read-only access to the public/sample workspace.", "amount": 0, "currency": "usd", "interval": None, "price_env": None, "features": ["Guest/sample cases", "Map-first dashboard", "Evidence summaries"]},
    "pro": {"id": "pro", "name": "Pro", "description": "Full investigation workspace for individual analysts.", "amount": 4900, "currency": "usd", "interval": "month", "price_env": "STRIPE_PRICE_PRO", "features": ["Unlimited case investigations", "AIS correlation and exports", "AI investigation tools"]},
    "institution": {"id": "institution", "name": "Institution", "description": "Team-ready access for maritime response organizations.", "amount": 19900, "currency": "usd", "interval": "month", "price_env": "STRIPE_PRICE_INSTITUTION", "features": ["Team workspace", "Priority ingestion", "Audit and evidence controls"]},
}

class CheckoutRequest(BaseModel):
    plan: str = Field(pattern="^(pro|institution)$")
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


def _stripe():
    key = os.getenv("STRIPE_SECRET_KEY")
    if not key:
        raise HTTPException(503, "Stripe billing is not configured")
    stripe.api_key = key
    return stripe


def _catalog():
    out = []
    for plan in PLANS.values():
        item = dict(plan)
        item["price_id_configured"] = bool(plan.get("price_env") and os.getenv(plan["price_env"]))
        item.pop("price_env", None)
        out.append(item)
    return out


async def current_entitlement(user: dict) -> dict:
    if user.get("role") == "guest":
        return {"plan": "viewer", "status": "active", "source": "guest", "features": PLANS["viewer"]["features"]}
    rec = await db.billing_entitlements.find_one({"user_id": user.get("id")}, {"_id": 0})
    if not rec:
        return {"plan": "viewer", "status": "active", "source": "sample", "features": PLANS["viewer"]["features"]}
    return clean({**rec, "features": PLANS.get(rec.get("plan"), PLANS["viewer"])["features"]})


def require_plan(min_plan: str):
    order = {"viewer": 0, "pro": 1, "institution": 2}
    async def dep(user=Depends(get_current_user)):
        ent = await current_entitlement(user)
        if ent.get("status") not in ("active", "trialing") or order.get(ent.get("plan"), 0) < order[min_plan]:
            raise HTTPException(402, {"code": "ENTITLEMENT_REQUIRED", "required": min_plan, "entitlement": ent})
        return user
    return dep


@router.get("/catalog")
async def catalog(user=Depends(get_current_user)):
    ent = await current_entitlement(user)
    return {"environment": os.getenv("STRIPE_ENV", "test"), "plans": _catalog(), "entitlement": ent}


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return await current_entitlement(user)


@router.post("/claim-viewer")
async def claim_viewer(user=Depends(get_current_user)):
    if user.get("role") == "guest":
        return await current_entitlement(user)
    now = datetime.now(timezone.utc)
    await db.billing_entitlements.update_one({"user_id": user["id"]}, {"$set": {"user_id": user["id"], "email": user.get("email"), "plan": "viewer", "status": "active", "source": "claimable-sandbox", "updated_at": now}}, upsert=True)
    await audit("billing", user["id"], "viewer.claimed", {"plan": "viewer"}, user.get("email", "system"))
    return await current_entitlement(user)


@router.post("/checkout")
async def checkout(body: CheckoutRequest, request: Request, user=Depends(get_current_user)):
    s = _stripe(); plan = PLANS[body.plan]; price_id = os.getenv(plan["price_env"])
    if not price_id:
        raise HTTPException(503, f"{plan['name']} price is not configured")
    origin = str(request.base_url).rstrip("/")
    success = body.success_url or f"{origin}/account?billing=success"
    cancel = body.cancel_url or f"{origin}/account?billing=cancelled"
    try:
        session = s.checkout.Session.create(mode="subscription", line_items=[{"price": price_id, "quantity": 1}], customer_email=user.get("email"), client_reference_id=user.get("id"), metadata={"user_id": user.get("id"), "plan": body.plan}, subscription_data={"metadata": {"user_id": user.get("id"), "plan": body.plan}}, success_url=success, cancel_url=cancel)
    except Exception as exc:
        raise HTTPException(502, f"Stripe checkout unavailable: {str(exc)[:160]}")
    return {"checkout_url": session.url, "session_id": session.id}


@router.post("/webhook")
async def webhook(request: Request):
    payload = await request.body(); signature = request.headers.get("stripe-signature"); secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret or not signature:
        raise HTTPException(400, "Missing Stripe webhook verification material")
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except ValueError:
        raise HTTPException(400, "Invalid webhook payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid webhook signature")
    obj = event["data"]["object"]; event_type = event["type"]
    metadata = obj.get("metadata", {}) or {}; user_id = metadata.get("user_id") or obj.get("client_reference_id")
    if event_type == "checkout.session.completed":
        user_id = user_id or metadata.get("user_id"); plan = metadata.get("plan", "pro"); status = "active"; customer = obj.get("customer"); subscription = obj.get("subscription")
    elif event_type in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        user_id = user_id or metadata.get("user_id"); plan = metadata.get("plan", "pro"); status = "canceled" if event_type.endswith("deleted") else obj.get("status", "active"); customer = obj.get("customer"); subscription = obj.get("id")
    else:
        return {"received": True, "ignored": True}
    if user_id:
        await db.billing_entitlements.update_one({"user_id": user_id}, {"$set": {"user_id": user_id, "plan": plan, "status": status, "source": "stripe", "stripe_customer_id": customer, "stripe_subscription_id": subscription, "updated_at": datetime.now(timezone.utc), "last_event_id": event.get("id")}}, upsert=True)
        await audit("billing", user_id, f"stripe.{event_type}", {"plan": plan, "status": status}, "stripe")
    return {"received": True}
