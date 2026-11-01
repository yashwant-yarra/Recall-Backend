import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from recall import auth, billing
from recall.config import settings

SECRET = "test_webhook_secret"


def signed(body: str, secret: str = SECRET) -> str:
    """An X-Razorpay-Signature header, built the way Razorpay builds it."""
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    # Only the billing router, no lifespan: these tests never reach the database or Razorpay.
    monkeypatch.setattr(settings, "razorpay_webhook_secret", SECRET)
    app = FastAPI()
    app.include_router(billing.router)
    app.dependency_overrides[auth.get_current_user_id] = lambda: "test-user"
    return TestClient(app)


def test_plan_for_status():
    assert billing.plan_for_status("active") == "pro"
    for status in ("created", "authenticated", "pending", "halted", "cancelled", "completed", "expired", None):
        assert billing.plan_for_status(status) == "free"


def test_valid_signature():
    body = b'{"event": "subscription.activated"}'
    assert billing.valid_signature(body, signed(body.decode()), SECRET)
    assert not billing.valid_signature(body, signed(body.decode(), "other_secret"), SECRET)
    assert not billing.valid_signature(body, None, SECRET)
    assert not billing.valid_signature(body, "", SECRET)


def test_webhook_without_signature_rejected(client):
    assert client.post("/billing/webhook", content='{"event": "x"}').status_code == 400


def test_webhook_wrong_secret_rejected(client):
    body = json.dumps({"event": "subscription.activated"})
    r = client.post("/billing/webhook", content=body, headers={"X-Razorpay-Signature": signed(body, "other_secret")})
    assert r.status_code == 400


def test_webhook_tampered_body_rejected(client):
    # Signed for one body, sent with another: someone edited the event in transit.
    original = json.dumps({"event": "subscription.cancelled"})
    forged = original.replace("cancelled", "activated")
    r = client.post("/billing/webhook", content=forged, headers={"X-Razorpay-Signature": signed(original)})
    assert r.status_code == 400


def test_billing_off_without_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", None)
    monkeypatch.setattr(settings, "razorpay_key_id", None)
    monkeypatch.setattr(settings, "razorpay_key_secret", None)
    assert client.post("/billing/webhook", content="{}").status_code == 503
    assert client.post("/billing/checkout").status_code == 503
