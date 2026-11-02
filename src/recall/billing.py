"""Cashfree billing: Pro and Ultra are prepaid passes, bought through Cashfree orders.

Flow: POST /billing/checkout {tier, phone} creates a Cashfree order for that tier's price and returns its
payment_session_id; the UI opens Cashfree's checkout as a popup over the page with it (card and UPI
details never touch this server). When the payment succeeds, Cashfree calls POST /billing/webhook; that
is the only place a pass is granted. Each purchase adds pass_days to users.paid_until, and the tier counts
while paid_until is in the future (db.TIER). Nothing renews, so there is nothing to cancel: the user buys
again to extend. The free trial (POST /billing/trial) needs no payment and lives in users.trial_ends_at.

No Cashfree SDK on the server: two calls (create and fetch an order), made with httpx (async), and the
webhook signature is a plain HMAC-SHA256, checked with the standard library.
"""
import base64
import hashlib
import hmac
import json
import logging
import uuid
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from recall import auth
from recall.abuse import trial_refusal
from recall.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


def price_for(tier: str) -> int:
    """The price of one pass, in rupees."""
    return settings.ultra_price_inr if tier == "ultra" else settings.pro_price_inr


def customer_id_for(user_id: str) -> str:
    """Cashfree's customer_id allows only letters and digits (3-50). A hash of the user ID always fits and
    stays the same per user, so Cashfree groups one user's payments together."""
    return "u" + hashlib.sha256(user_id.encode()).hexdigest()[:40]


def pass_from_order(order: dict) -> tuple[str, str] | None:
    """(user_id, tier) for an order Cashfree reports as PAID with the tags this server set; otherwise None."""
    tags = order.get("order_tags") or {}
    user_id, tier = tags.get("user_id"), tags.get("tier")
    if order.get("order_status") != "PAID" or not user_id or tier not in ("pro", "ultra"):
        return None
    return user_id, tier


class CheckoutRequest(BaseModel):
    tier: Literal["pro", "ultra"]
    # Cashfree requires a customer phone on every order: a 10-digit Indian mobile number, digits only.
    phone: str = Field(pattern=r"^[6-9]\d{9}$")


def valid_signature(body: bytes, timestamp: str | None, signature: str | None, secret: str) -> bool:
    """Cashfree signs the timestamp header plus the raw body: base64(HMAC-SHA256(client_secret, ts + body))."""
    if not (timestamp and signature):
        return False
    digest = hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)  # constant time: no timing leak


def _require_keys() -> tuple[str, str]:
    if not (settings.cashfree_client_id and settings.cashfree_client_secret and settings.cashfree_notify_url):
        raise HTTPException(status_code=503, detail="Billing is not configured")
    return settings.cashfree_client_id, settings.cashfree_client_secret


async def _cashfree(method: str, path: str, json: dict | None = None) -> dict:
    """One Cashfree API call, authenticated with the client ID and secret headers."""
    client_id, secret = _require_keys()
    headers = {"x-client-id": client_id, "x-client-secret": secret, "x-api-version": settings.cashfree_api_version}
    async with httpx.AsyncClient(base_url=settings.cashfree_api_url, headers=headers, timeout=15) as client:
        resp = await client.request(method, path, json=json)
    if resp.status_code >= 400:
        logger.error("Cashfree %s %s failed: %s %s", method, path, resp.status_code, resp.text[:500])
        raise HTTPException(status_code=502, detail="Payment provider error")
    return resp.json()


@router.post("/billing/checkout")
async def checkout(body: CheckoutRequest, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """Buy a pass. Buying your current tier extends it; Pro can move up to Ultra. Pro can't be bought while
    an Ultra pass is running (it would only waste the money: the webhook keeps Ultra)."""
    _require_keys()
    async with request.app.state.engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT plan, paid_until, paid_until > now() AS active FROM users WHERE id = :uid"), {"uid": user_id})).one()
    if row.active and row.plan == "ultra" and body.tier == "pro":
        ends = row.paid_until.date().isoformat()
        raise HTTPException(status_code=409, detail=f"You have Ultra until {ends}. Pro can be bought after that.")

    # user_id and tier go in the order's tags, set by this server. The webhook reads them back from
    # Cashfree's copy of the order, so they can be trusted to say who paid and for what. The order ID is
    # also the idempotency key.
    meta = {"notify_url": settings.cashfree_notify_url}
    if settings.cashfree_return_url:
        # Used only when the popup can't open (some in-app browsers) and Cashfree redirects instead.
        meta["return_url"] = settings.cashfree_return_url
    order = await _cashfree("POST", "/orders", json={
        "order_id": f"recall_{uuid.uuid4().hex}",
        "order_amount": price_for(body.tier),
        "order_currency": "INR",
        "order_note": f"Recall {body.tier.capitalize()}, {settings.pass_days} days",
        "customer_details": {"customer_id": customer_id_for(user_id), "customer_phone": body.phone},
        "order_tags": {"user_id": user_id, "tier": body.tier},
        "order_meta": meta,
    })
    # mode tells the UI's Cashfree script which environment the session belongs to.
    mode = "sandbox" if "sandbox" in settings.cashfree_api_url else "production"
    return {"order_id": order["order_id"], "payment_session_id": order["payment_session_id"], "mode": mode}


@router.post("/billing/trial")
async def start_trial(request: Request, who: dict = Depends(auth.identity_from_request)):
    """Start the account's one free trial (Pro, the middle tier). Works without Cashfree (no card), so no
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


@router.post("/billing/webhook")
async def webhook(request: Request):
    """Called by Cashfree, not by users: no login, but every request must carry a valid signature."""
    secret = settings.cashfree_client_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Billing is not configured")
    body = await request.body()  # the raw bytes: the signature covers them exactly
    h = request.headers
    if not valid_signature(body, h.get("x-webhook-timestamp"), h.get("x-webhook-signature"), secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    event = json.loads(body)
    data = event.get("data") or {}
    order_id = (data.get("order") or {}).get("order_id")
    payment_status = (data.get("payment") or {}).get("payment_status")
    if event.get("type") != "PAYMENT_SUCCESS_WEBHOOK" or payment_status != "SUCCESS" or not order_id:
        return {"status": "ignored"}  # failed, dropped, pending, or another product's event

    # Fetch the order instead of trusting the event's copy: older webhook versions send order_tags as null,
    # and Cashfree's own order_status is the documented proof of payment.
    paid = pass_from_order(await _cashfree("GET", f"/orders/{order_id}"))
    if paid is None:
        logger.warning("Cashfree order %s is not paid or has no valid user_id/tier tags", order_id)
        return {"status": "ignored"}
    user_id, tier = paid

    async with request.app.state.engine.begin() as conn:
        # Idempotency: an order is paid once, so its ID is the event's key. A redelivery (Cashfree delivers
        # at least once) or a replayed request matches the primary key and is skipped. The pass is granted in
        # the same transaction, so a failure means Cashfree retries both.
        new = (await conn.execute(text(
            "INSERT INTO payment_events (id, type) VALUES (:id, 'order.paid') ON CONFLICT (id) DO NOTHING RETURNING id"
        ), {"id": order_id})).scalar_one_or_none()
        if new is None:
            return {"status": "duplicate"}
        # The days are added to whatever is left (GREATEST skips a NULL paid_until). A running Ultra pass
        # stays Ultra even if this payment was for Pro (an old order paid late), so a payment never lowers
        # the tier. ponytail: Pro days left at an upgrade become Ultra days; prorate if that costs too much.
        await conn.execute(text(
            "UPDATE users SET "
            "plan = CASE WHEN plan = 'ultra' AND paid_until > now() THEN 'ultra' ELSE :tier END, "
            "paid_until = GREATEST(paid_until, now()) + make_interval(days => :days) "
            "WHERE id = :uid"
        ), {"tier": tier, "days": settings.pass_days, "uid": user_id})
    logger.info("Cashfree order %s paid: %s pass for user %s", order_id, tier, user_id)
    return {"status": "ok"}
