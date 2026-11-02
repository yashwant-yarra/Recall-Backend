import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from recall import auth
from recall.auth import bearer_token, verify_token


def test_bearer_token():
    assert bearer_token("Bearer abc.def") == "abc.def"
    assert bearer_token("bearer abc") == "abc"  # the scheme is case-insensitive


def test_no_or_wrong_header():
    for value in (None, "", "Bearer", "Bearer   ", "Basic abc", "abc"):
        assert bearer_token(value) is None


def test_garbage_token_rejected():
    # Must raise ValueError (-> 401), never return claims. Fetches Google's public keys first.
    with pytest.raises(ValueError):
        verify_token("not-a-jwt")


def _as(monkeypatch, email, verified):
    """Make verify_token return claims for `email`, with no network and no database."""
    monkeypatch.setattr(auth, "verify_token", lambda token: {"sub": "u1", "email": email, "email_verified": verified})
    monkeypatch.setattr(auth, "_ensure_user", AsyncMock())
    auth._known_users.discard("u1")


def test_unverified_email_is_refused(monkeypatch):
    _as(monkeypatch, "a@example.com", False)
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth.user_from_token("t", None))
    assert e.value.status_code == 403 and "Verify your email" in e.value.detail


def test_verified_email_is_allowed(monkeypatch):
    _as(monkeypatch, "a@example.com", True)
    assert asyncio.run(auth.user_from_token("t", None)) == "u1"


def test_review_account_is_allowed_without_verification(monkeypatch):
    for email in ("test@genai.dev", "Test@GenAI.dev"):  # the address is not case-sensitive
        _as(monkeypatch, email, False)
        assert asyncio.run(auth.user_from_token("t", None)) == "u1"
    _as(monkeypatch, "other@genai.dev", False)  # only that address, not its domain
    with pytest.raises(HTTPException):
        asyncio.run(auth.user_from_token("t", None))


def test_me_can_read_an_unverified_account(monkeypatch):
    _as(monkeypatch, "a@example.com", False)
    assert asyncio.run(auth.user_from_token("t", None, require_verified=False)) == "u1"
