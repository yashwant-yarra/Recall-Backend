"""Razorpay billing: a monthly Pro subscription that raises the daily limits.

Flow: POST /billing/checkout creates a Razorpay subscription and returns its hosted payment link
(card details never touch this server). After payment, Razorpay calls POST /billing/webhook; that
is the only place users.plan changes.

No Razorpay SDK: the REST API is two calls (create and fetch a subscription), made with httpx
(async), and the webhook signature is a plain HMAC-SHA256, checked with the standard library.
"""
import hashlib
import hmac
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text

from recall import auth
from recall.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# Subscription statuses that grant Pro. ponytail: 'pending' (a failed renewal Razorpay is still
# retrying) drops to free immediately; add it here for a grace period.
PRO_STATUSES = {"active"}


def plan_for_status(status: str | None) -> str:
    return "pro" if status in PRO_STATUSES else "free"


def valid_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Razorpay signs the raw request body: hex(HMAC-SHA256(webhook_secret, body))."""
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)  # constant time: no timing leak


def _require_keys() -> tuple[str, str]:
    if not (settings.razorpay_key_id and settings.razorpay_key_secret):
        raise HTTPException(status_code=503, detail="Billing is not configured")
    return settings.razorpay_key_id, settings.razorpay_key_secret


async def _razorpay(method: str, path: str, json: dict | None = None) -> dict:
    """One Razorpay API call, authenticated with the key ID and secret (HTTP basic auth)."""
    async with httpx.AsyncClient(base_url=settings.razorpay_api_url, auth=_require_keys(), timeout=15) as client:
        resp = await client.request(method, path, json=json)
    if resp.status_code >= 400:
        logger.error("Razorpay %s %s failed: %s %s", method, path, resp.status_code, resp.text[:500])
        raise HTTPException(status_code=502, detail="Payment provider error")
    return resp.json()


@router.post("/billing/checkout")
async def checkout(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    _require_keys()
    if not settings.razorpay_plan_id:
        raise HTTPException(status_code=503, detail="Billing is not configured")
    async with request.app.state.engine.connect() as conn:
        plan = (await conn.execute(text("SELECT plan FROM users WHERE id = :uid"), {"uid": user_id})).scalar_one()
    if plan == "pro":
        raise HTTPException(status_code=409, detail="Already on Pro")

    # user_id goes in notes, set by this server. Webhooks are signed, so notes in a verified event
    # can be trusted to say which user a subscription belongs to.
    sub = await _razorpay("POST", "/subscriptions", json={
        "plan_id": settings.razorpay_plan_id,
        "total_count": settings.razorpay_total_count,
        "quantity": 1,
        "notes": {"user_id": user_id},
    })
    return {"subscription_id": sub["id"], "url": sub["short_url"]}


@router.post("/billing/cancel")
async def cancel(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """Stop Pro from renewing: the user keeps Pro until the month they paid for ends. users.plan is NOT
    changed here: when the period ends Razorpay sends subscription.cancelled and the webhook (still the
    only place the plan changes) moves the user to free."""
    _require_keys()
    sid = await _pro_subscription_id(request, user_id)
    # cancel_at_cycle_end = 1: the subscription stays 'active' (so Pro stays) until the period ends.
    sub = await _razorpay("POST", f"/subscriptions/{sid}/cancel", json={"cancel_at_cycle_end": 1})
    return {"cancel_scheduled": True, "ends_at": sub.get("current_end")}


@router.get("/billing/subscription")
async def subscription(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """Whether a Pro user's subscription is set to end, and when (unix seconds), so the UI can say so."""
    _require_keys()
    sid = await _pro_subscription_id(request, user_id)
    sub = await _razorpay("GET", f"/subscriptions/{sid}")
    return {"cancel_scheduled": bool(sub.get("has_scheduled_changes")), "ends_at": sub.get("current_end")}


async def _pro_subscription_id(request: Request, user_id: str) -> str:
    async with request.app.state.engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT plan, razorpay_subscription_id FROM users WHERE id = :uid"), {"uid": user_id}
        )).one()
    if row.plan != "pro" or not row.razorpay_subscription_id:
        raise HTTPException(status_code=409, detail="Not on Pro")
    return row.razorpay_subscription_id


@router.post("/billing/webhook")
async def webhook(request: Request):
    """Called by Razorpay, not by users: no login, but every request must carry a valid signature."""
    if not settings.razorpay_webhook_secret:
        raise HTTPException(status_code=503, detail="Billing is not configured")
    body = await request.body()  # the raw bytes: the signature covers them exactly
    if not valid_signature(body, request.headers.get("x-razorpay-signature"), settings.razorpay_webhook_secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    event = await request.json()
    event_type = event.get("event", "")
    # Razorpay sends a unique ID per event; fall back to the body's hash if it's ever missing.
    event_id = request.headers.get("x-razorpay-event-id") or hashlib.sha256(body).hexdigest()

    sub = None
    if event_type.startswith("subscription."):
        sub_id = event["payload"]["subscription"]["entity"]["id"]
        # Events can arrive out of order (an old 'charged' after 'cancelled'), so fetch the
        # subscription's current state instead of trusting the snapshot inside the event.
        sub = await _razorpay("GET", f"/subscriptions/{sub_id}")

    async with request.app.state.engine.begin() as conn:
        # Idempotency: a redelivered event ID matches the primary key and is skipped. The plan update
        # commits in the same transaction, so a failure means Razorpay retries both.
        new = (await conn.execute(text(
            "INSERT INTO payment_events (id, type) VALUES (:id, :type) ON CONFLICT (id) DO NOTHING RETURNING id"
        ), {"id": event_id, "type": event_type})).scalar_one_or_none()
        if new is None:
            return {"status": "duplicate"}
        if sub is not None:
            user_id = (sub.get("notes") or {}).get("user_id")
            if not user_id:
                logger.warning("Razorpay subscription %s has no user_id note", sub["id"])
                return {"status": "ignored"}
            if plan_for_status(sub["status"]) == "pro":
                # An active subscription makes the user Pro and becomes their current subscription.
                await conn.execute(text(
                    "UPDATE users SET plan = 'pro', subscription_status = :status, razorpay_subscription_id = :sid "
                    "WHERE id = :uid"
                ), {"status": sub["status"], "sid": sub["id"], "uid": user_id})
            else:
                # Only the user's current subscription can downgrade them: an old, abandoned one
                # being cancelled must not take Pro away.
                await conn.execute(text(
                    "UPDATE users SET plan = 'free', subscription_status = :status "
                    "WHERE id = :uid AND razorpay_subscription_id = :sid"
                ), {"status": sub["status"], "sid": sub["id"], "uid": user_id})
            logger.info("Razorpay %s: subscription %s is %s for user %s", event_type, sub["id"], sub["status"], user_id)
    return {"status": "ok"}
