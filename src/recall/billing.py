"""Razorpay billing: monthly Pro and Ultra subscriptions that raise the daily limits.

Flow: POST /billing/checkout {tier} creates a Razorpay subscription on that tier's plan and returns its
hosted payment link (card details never touch this server). After payment, Razorpay calls
POST /billing/webhook; that is the only place users.plan changes, and the subscription's plan says
which tier it buys. The free trial (POST /billing/trial) needs no payment and lives in users.trial_ends_at.

No Razorpay SDK: the REST API is two calls (create and fetch a subscription), made with httpx
(async), and the webhook signature is a plain HMAC-SHA256, checked with the standard library.
"""
import hashlib
import hmac
import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

from recall import auth
from recall.abuse import trial_refusal
from recall.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# Subscription statuses that grant a paid tier. ponytail: 'pending' (a failed renewal Razorpay is still
# retrying) drops to free immediately; add it here for a grace period.
PAID_STATUSES = {"active"}


def plan_id_for(tier: str) -> str | None:
    """The Razorpay plan that sells a tier: RAZORPAY_PRO_PLAN_ID for Pro, RAZORPAY_PLAN_ID for Ultra."""
    return settings.razorpay_pro_plan_id if tier == "pro" else settings.razorpay_plan_id


def tier_for_plan_id(plan_id: str | None) -> str:
    """Which tier a subscription buys, from its Razorpay plan. Only the Ultra plan gives Ultra; anything
    else, including an unknown plan, gives Pro, so a mix-up can never hand out the higher tier by accident."""
    return "ultra" if settings.razorpay_plan_id and plan_id == settings.razorpay_plan_id else "pro"


def plan_for_subscription(status: str | None, plan_id: str | None) -> str:
    """The users.plan a subscription gives: its tier while active, otherwise free."""
    return tier_for_plan_id(plan_id) if status in PAID_STATUSES else "free"


class CheckoutRequest(BaseModel):
    tier: Literal["pro", "ultra"]


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
async def checkout(body: CheckoutRequest, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """Start paying for a tier. Free can buy either; Pro can move up to Ultra (the webhook then cancels the
    Pro subscription). Moving down is not offered: switch to Free first, then choose Pro."""
    _require_keys()
    plan_id = plan_id_for(body.tier)
    if not plan_id:
        raise HTTPException(status_code=503, detail=f"{body.tier.capitalize()} is not available yet")
    async with request.app.state.engine.connect() as conn:
        plan = (await conn.execute(text("SELECT plan FROM users WHERE id = :uid"), {"uid": user_id})).scalar_one()
    if plan == body.tier:
        raise HTTPException(status_code=409, detail=f"Already on {plan.capitalize()}")
    if plan == "ultra":
        raise HTTPException(status_code=409, detail="To move to Pro, switch to Free first, then choose Pro")

    # user_id goes in notes, set by this server. Webhooks are signed, so notes in a verified event
    # can be trusted to say which user a subscription belongs to.
    sub = await _razorpay("POST", "/subscriptions", json={
        "plan_id": plan_id,
        "total_count": settings.razorpay_total_count,
        "quantity": 1,
        "notes": {"user_id": user_id},
    })
    return {"subscription_id": sub["id"], "url": sub["short_url"]}


@router.post("/billing/trial")
async def start_trial(request: Request, who: dict = Depends(auth.identity_from_request)):
    """Start the account's one free trial (Pro, the middle tier). Works without Razorpay (no card), so no
    keys needed. The email must be verified and not a throwaway address (abuse.trial_refusal). One
    statement checks and sets: only a free account that never had a trial matches, so a double click or a
    second attempt matches nothing."""
    user_id = who["user_id"]
    refusal = trial_refusal(who["email"], who["email_verified"])
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    async with request.app.state.engine.begin() as conn:
        ends_at = (await conn.execute(text(
            "UPDATE users SET trial_ends_at = now() + make_interval(days => :days) "
            "WHERE id = :uid AND plan = 'free' AND trial_ends_at IS NULL RETURNING trial_ends_at"
        ), {"uid": user_id, "days": settings.trial_days})).scalar_one_or_none()
    if ends_at is None:
        raise HTTPException(status_code=409, detail="The free trial was already used on this account")
    return {"ends_at": ends_at.isoformat()}


@router.post("/billing/cancel")
async def cancel(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """Stop Pro from renewing: the user keeps Pro until the month they paid for ends. users.plan is NOT
    changed here: when the period ends Razorpay sends subscription.cancelled and the webhook (still the
    only place the plan changes) moves the user to free."""
    _require_keys()
    sid = await _paid_subscription_id(request, user_id)
    # cancel_at_cycle_end = 1: the subscription stays 'active' (so the tier stays) until the period ends.
    sub = await _razorpay("POST", f"/subscriptions/{sid}/cancel", json={"cancel_at_cycle_end": 1})
    return {"cancel_scheduled": True, "ends_at": sub.get("current_end")}


@router.get("/billing/subscription")
async def subscription(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """Whether a Pro user's subscription is set to end, and when (unix seconds), so the UI can say so."""
    _require_keys()
    sid = await _paid_subscription_id(request, user_id)
    sub = await _razorpay("GET", f"/subscriptions/{sid}")
    return {"cancel_scheduled": bool(sub.get("has_scheduled_changes")), "ends_at": sub.get("current_end")}


async def _paid_subscription_id(request: Request, user_id: str) -> str:
    async with request.app.state.engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT plan, razorpay_subscription_id FROM users WHERE id = :uid"), {"uid": user_id}
        )).one()
    if row.plan == "free" or not row.razorpay_subscription_id:
        raise HTTPException(status_code=409, detail="Not on a paid plan")
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
    replaced = None  # the subscription an upgrade replaces, cancelled after the plan change commits
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
            tier = plan_for_subscription(sub["status"], sub.get("plan_id"))
            if tier != "free":
                # An active subscription gives the user its tier and becomes their current subscription.
                previous = (await conn.execute(text(
                    "SELECT razorpay_subscription_id FROM users WHERE id = :uid FOR UPDATE"
                ), {"uid": user_id})).scalar_one_or_none()
                await conn.execute(text(
                    "UPDATE users SET plan = :tier, subscription_status = :status, razorpay_subscription_id = :sid "
                    "WHERE id = :uid"
                ), {"tier": tier, "status": sub["status"], "sid": sub["id"], "uid": user_id})
                if previous and previous != sub["id"]:
                    replaced = previous  # an upgrade: the old subscription must stop billing
            else:
                # Only the user's current subscription can downgrade them: an old, abandoned one
                # being cancelled must not take Pro away.
                await conn.execute(text(
                    "UPDATE users SET plan = 'free', subscription_status = :status "
                    "WHERE id = :uid AND razorpay_subscription_id = :sid"
                ), {"status": sub["status"], "sid": sub["id"], "uid": user_id})
            logger.info("Razorpay %s: subscription %s is %s for user %s", event_type, sub["id"], sub["status"], user_id)
    if replaced:
        # After the plan change is committed. Its own 'cancelled' event is then ignored (it is no longer
        # the current subscription). If this call fails the user keeps the new tier; the old one has to
        # be cancelled in the Razorpay dashboard, so say so loudly.
        try:
            await _razorpay("POST", f"/subscriptions/{replaced}/cancel", json={"cancel_at_cycle_end": 0})
        except HTTPException:
            logger.error("Upgrade for user %s: old subscription %s was NOT cancelled; cancel it in Razorpay", user_id, replaced)
    return {"status": "ok"}
