import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from recall import auth, billing
from recall.config import settings
from recall.db import paid_plan

SECRET = "test_client_secret"
TS = "1730000000"


def signed(body: str, ts: str = TS, secret: str = SECRET) -> str:
    """An x-webhook-signature header, built the way Cashfree builds it: base64(HMAC-SHA256(secret, ts + body))."""
    return base64.b64encode(hmac.new(secret.encode(), (ts + body).encode(), hashlib.sha256).digest()).decode()


def headers(body: str, **kw) -> dict:
    return {"x-webhook-timestamp": kw.get("ts", TS), "x-webhook-signature": signed(body, **kw)}


@pytest.fixture
def client(monkeypatch):
    # Only the billing router, no lifespan: these tests never reach the database or Cashfree.
    monkeypatch.setattr(settings, "cashfree_client_secret", SECRET)
    app = FastAPI()
    app.include_router(billing.router)
    app.dependency_overrides[auth.get_current_user_id] = lambda: "test-user"
    return TestClient(app)


def test_valid_signature():
    body = b'{"type": "PAYMENT_LINK_EVENT"}'
    assert billing.valid_signature(body, TS, signed(body.decode()), SECRET)
    assert not billing.valid_signature(body, TS, signed(body.decode(), secret="other"), SECRET)
    assert not billing.valid_signature(body, "1730000001", signed(body.decode()), SECRET)  # timestamp is signed too
    assert not billing.valid_signature(body, None, signed(body.decode()), SECRET)
    assert not billing.valid_signature(body, TS, None, SECRET)
    assert not billing.valid_signature(body, TS, "", SECRET)


def test_paid_plan_counts_only_until_paid_until():
    now = datetime.now(timezone.utc)
    assert paid_plan("pro", now + timedelta(days=1), now) == "pro"
    assert paid_plan("ultra", now + timedelta(days=1), now) == "ultra"
    assert paid_plan("pro", now - timedelta(seconds=1), now) == "free"  # the pass ran out
    assert paid_plan("pro", None, now) == "free"
    assert paid_plan("free", now + timedelta(days=1), now) == "free"


def test_price_for(monkeypatch):
    monkeypatch.setattr(settings, "pro_price_inr", 699)
    monkeypatch.setattr(settings, "ultra_price_inr", 1999)
    assert billing.price_for("pro") == 699
    assert billing.price_for("ultra") == 1999


def test_webhook_without_signature_rejected(client):
    assert client.post("/billing/webhook", content='{"type": "x"}').status_code == 400


def test_webhook_wrong_secret_rejected(client):
    body = json.dumps({"type": "PAYMENT_LINK_EVENT"})
    assert client.post("/billing/webhook", content=body, headers=headers(body, secret="other")).status_code == 400


def test_webhook_tampered_body_rejected(client):
    # Signed for one body, sent with another: someone edited the event in transit.
    original = json.dumps({"type": "PAYMENT_FAILED_WEBHOOK", "data": {"payment": {"payment_status": "FAILED"}}})
    forged = original.replace("FAILED", "SUCCESS")
    assert client.post("/billing/webhook", content=forged, headers=headers(original)).status_code == 400


def test_webhook_ignores_other_events_without_calling_out(client):
    # The test app has no engine and no Cashfree keys: calling either would fail, so 200 proves it stopped early.
    for kind, status in (("PAYMENT_FAILED_WEBHOOK", "FAILED"), ("PAYMENT_USER_DROPPED_WEBHOOK", "USER_DROPPED"),
                         ("PAYMENT_SUCCESS_WEBHOOK", "PENDING"), ("PAYMENT_LINK_EVENT", None)):
        body = json.dumps({"type": kind, "data": {"order": {"order_id": "o1"}, "payment": {"payment_status": status}}})
        r = client.post("/billing/webhook", content=body, headers=headers(body))
        assert r.status_code == 200 and r.json()["status"] == "ignored"


def test_pass_from_order():
    # The webhook trusts only the order as Cashfree reports it: paid, with the tags this server set.
    paid = {"order_status": "PAID", "order_tags": {"user_id": "u1", "tier": "ultra"}}
    assert billing.pass_from_order(paid) == ("u1", "ultra")
    assert billing.pass_from_order({**paid, "order_status": "ACTIVE"}) is None  # not paid (yet)
    for tags in (None, {}, {"user_id": "u1"}, {"user_id": "u1", "tier": "gold"}, {"tier": "pro"}):
        assert billing.pass_from_order({"order_status": "PAID", "order_tags": tags}) is None


def test_customer_id_is_stable_and_alphanumeric():
    # Cashfree allows only letters and digits, 3-50 characters; user IDs can hold '-' (dev-user).
    a = billing.customer_id_for("dev-user")
    assert a == billing.customer_id_for("dev-user") and a.isalnum() and 3 <= len(a) <= 50
    assert a != billing.customer_id_for("other-user")


def test_checkout_validates_phone(client, monkeypatch):
    monkeypatch.setattr(settings, "cashfree_client_id", "id")
    for phone in ("12345", "5123456789", "98765432101", "+919876543210", ""):
        assert client.post("/billing/checkout", json={"tier": "pro", "phone": phone}).status_code == 422
    assert client.post("/billing/checkout", json={"tier": "pro"}).status_code == 422


def test_billing_off_without_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "cashfree_client_id", None)
    monkeypatch.setattr(settings, "cashfree_client_secret", None)
    assert client.post("/billing/webhook", content="{}").status_code == 503
    assert client.post("/billing/checkout", json={"tier": "pro", "phone": "9876543210"}).status_code == 503
    assert client.post("/billing/checkout", json={"tier": "gold", "phone": "9876543210"}).status_code == 422


def test_cancel_and_subscription_are_gone(client):
    # Passes don't renew, so there is nothing to cancel or look up.
    assert client.post("/billing/cancel").status_code == 404
    assert client.get("/billing/subscription").status_code == 404
