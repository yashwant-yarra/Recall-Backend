import pytest

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
