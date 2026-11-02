"""Cashfree billing: Pro and Ultra are prepaid passes, bought through payment links.

Flow: POST /billing/checkout {tier, phone} creates a Cashfree payment link for that tier's price and
returns its URL (card and UPI details never touch this server). When the link is paid, Cashfree calls
POST /billing/webhook; that is the only place a pass is granted. Each purchase adds pass_days to
users.paid_until, and the tier counts while paid_until is in the future (db.TIER). Nothing renews, so
there is nothing to cancel: the user buys again to extend. The free trial (POST /billing/trial) needs no
payment and lives in users.trial_ends_at.

No Cashfree SDK: the API is one call (create a link), made with httpx (async), and the webhook signature
is a plain HMAC-SHA256, checked with the standard library.
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


class CheckoutRequest(BaseModel):
    tier: Literal["pro", "ultra"]
    # Cashfree requires a customer phone on every link: a 10-digit Indian mobile number, digits only.
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

    # user_id and tier go in the link's notes, set by this server. Webhooks are signed, so notes in a
    # verified event can be trusted to say who paid and for what. The link ID is also the idempotency key.
    link = await _cashfree("POST", "/links", json={
        "link_id": f"recall_{uuid.uuid4().hex}",
        "link_amount": price_for(body.tier),
        "link_currency": "INR",
        "link_purpose": f"Recall {body.tier.capitalize()}, {settings.pass_days} days",
        "customer_details": {"customer_phone": body.phone},
        "link_notes": {"user_id": user_id, "tier": body.tier},
        "link_meta": {"notify_url": settings.cashfree_notify_url},
    })
    return {"link_id": link["link_id"], "url": link["link_url"]}


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
    if event.get("type") != "PAYMENT_LINK_EVENT" or data.get("link_status") != "PAID":
        return {"status": "ignored"}  # partly paid, expired, cancelled, or another product's event
    notes = data.get("link_notes") or {}
    user_id, tier, link_id = notes.get("user_id"), notes.get("tier"), data.get("link_id")
    if not user_id or tier not in ("pro", "ultra") or not link_id:
        logger.warning("Cashfree paid link %s has no valid user_id/tier notes", link_id)
        return {"status": "ignored"}

    async with request.app.state.engine.begin() as conn:
        # Idempotency: a link is paid once, so its ID is the event's key. A redelivery (Cashfree delivers at
        # least once) or a replayed request matches the primary key and is skipped. The pass is granted in
        # the same transaction, so a failure means Cashfree retries both.
        new = (await conn.execute(text(
            "INSERT INTO payment_events (id, type) VALUES (:id, 'link.paid') ON CONFLICT (id) DO NOTHING RETURNING id"
        ), {"id": link_id})).scalar_one_or_none()
        if new is None:
            return {"status": "duplicate"}
        # The days are added to whatever is left (GREATEST skips a NULL paid_until). A running Ultra pass
        # stays Ultra even if this payment was for Pro (an old link paid late), so a payment never lowers
        # the tier. ponytail: Pro days left at an upgrade become Ultra days; prorate if that costs too much.
        await conn.execute(text(
            "UPDATE users SET "
            "plan = CASE WHEN plan = 'ultra' AND paid_until > now() THEN 'ultra' ELSE :tier END, "
            "paid_until = GREATEST(paid_until, now()) + make_interval(days => :days) "
            "WHERE id = :uid"
        ), {"tier": tier, "days": settings.pass_days, "uid": user_id})
    logger.info("Cashfree link %s paid: %s pass for user %s", link_id, tier, user_id)
    return {"status": "ok"}
