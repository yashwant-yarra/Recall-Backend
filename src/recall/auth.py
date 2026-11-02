import asyncio
import logging

from fastapi import HTTPException, Request
from google.auth import exceptions as google_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .abuse import is_verification_exempt
from .config import settings

logger = logging.getLogger(__name__)

_transport = google_requests.Request()  # one HTTP session for fetching Google's public signing keys
_known_users: set[str] = set()  # user IDs this process has already made sure exist in `users`


def bearer_token(authorization: str | None) -> str | None:
    """The token from an 'Authorization: Bearer <token>' header, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def verify_token(token: str) -> dict:
    """A Firebase ID token's claims. Checks the signature, expiry, issuer and that it was issued
    for this project (audience). Raises ValueError for any bad token.

    ponytail: google-auth fetches Google's public keys on every call (one extra HTTP request per
    API call); cache them for their max-age if latency matters.
    """
    return id_token.verify_firebase_token(token, _transport, audience=settings.google_cloud_project)


async def _ensure_user(engine, user_id: str, email: str) -> None:
    """Create the users row on first sight: files, chats and agent_runs all reference users(id)."""
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO users (id, email) VALUES (:id, :email) ON CONFLICT (id) DO NOTHING"
            ), {"id": user_id, "email": email})
    except IntegrityError:  # the email belongs to a different user ID (e.g. a deleted, re-made account)
        raise HTTPException(status_code=409, detail="Email already linked to another account")


async def get_current_user_id(request: Request) -> str:
    return await user_from_token(bearer_token(request.headers.get("authorization")), request.app.state.engine)


async def user_from_token(token: str | None, engine, require_verified: bool = True) -> str:
    """The user ID for a Firebase ID token, or HTTPException 401/403/409/503. Shared by HTTP routes
    (token from the Authorization header) and the live WebSocket (token from its first message).
    Unverified emails get 403 unless require_verified is False (only /me, which the UI needs to ask them
    to verify) or the account is on the exempt list."""
    if token is None:
        # Local development only. DEV_USER_ID is never set on Cloud Run, so production requires a
        # token and fails closed.
        if settings.dev_user_id is not None:
            return settings.dev_user_id
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        claims = await asyncio.to_thread(verify_token, token)  # blocking: may fetch keys over HTTP
    except google_exceptions.TransportError:
        logger.exception("Could not fetch Firebase public keys")
        raise HTTPException(status_code=503, detail="Authentication temporarily unavailable")
    except ValueError:  # bad signature, expired, wrong project, malformed
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id, email = claims["sub"], claims.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Account has no email")
    if require_verified and not claims.get("email_verified") and not is_verification_exempt(email):
        raise HTTPException(status_code=403, detail="Verify your email address to use Recall.")
    # ponytail: per-process memory; each new instance re-checks a user once, which is harmless.
    if user_id not in _known_users:
        await _ensure_user(engine, user_id, email)
        _known_users.add(user_id)
    return user_id


async def identity_from_request(request: Request) -> dict:
    """Who is calling: {"user_id", "email", "email_verified"}. For routes that need more than the user ID
    (the free trial checks the email). Verifies the token exactly like get_current_user_id, then reads
    its claims; with no token (local development only) the dev user counts as verified.
    ponytail: the token is verified twice (once here, once for the user); cache the claims if it matters."""
    token = bearer_token(request.headers.get("authorization"))
    user_id = await user_from_token(token, request.app.state.engine, require_verified=False)
    if token is None:
        return {"user_id": user_id, "email": None, "email_verified": True}
    claims = await asyncio.to_thread(verify_token, token)
    email = claims.get("email")
    verified = bool(claims.get("email_verified")) or is_verification_exempt(email)
    return {"user_id": user_id, "email": email, "email_verified": verified}
