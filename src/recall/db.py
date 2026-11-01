from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from .config import settings

# SQL for the start of the current UTC day, as a timestamptz. All daily limits use it, so they agree
# on when a day starts regardless of the server's or the session's time zone.
UTC_DAY_START = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"

# SQL for "this user has Pro access": a paid plan, or a free trial that has not ended yet. Used on the
# users table (or an alias of it); NULL (no trial) counts as no access. The one definition of Pro access.
PRO_ACCESS = "(plan = 'pro' OR trial_ends_at > now())"

# SQL for a user's daily limit: the :pro value if they have Pro access, else :free.
# Every query that uses it binds :uid, :free and :pro.
PLAN_LIMIT = f"(CASE WHEN COALESCE((SELECT {PRO_ACCESS} FROM users WHERE id = :uid), false) THEN :pro ELSE :free END)"


async def is_pro(engine: AsyncEngine, user_id: str) -> bool:
    """Pro access: a paid plan or a running free trial."""
    async with engine.connect() as conn:
        access = (await conn.execute(text(f"SELECT {PRO_ACCESS} FROM users WHERE id = :uid"), {"uid": user_id})).scalar_one_or_none()
    return access is True


def trial_state(plan: str, trial_ends_at: datetime | None, now: datetime) -> dict:
    """What the UI needs about the free trial. available = never started and not already paying."""
    return {
        "active": trial_ends_at is not None and trial_ends_at > now,
        "ends_at": trial_ends_at.isoformat() if trial_ends_at else None,
        "available": trial_ends_at is None and plan != "pro",
    }


async def require_pro(engine: AsyncEngine, user_id: str, feature: str) -> None:
    """Refuse Free users with a plain message, before any work or quota is spent on their request."""
    if not await is_pro(engine, user_id):
        raise HTTPException(status_code=403, detail=f"{feature} is part of Pro")


def make_engine() -> AsyncEngine:
    # prepare_threshold=None: Neon's pooler (PgBouncer, transaction mode) breaks server-side prepared statements.
    connect_args = {"prepare_threshold": None}
    return create_async_engine(settings.database_url, echo=settings.db_echo, pool_pre_ping=True, connect_args=connect_args)