from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from .config import settings

# SQL for the start of the current UTC day, as a timestamptz. All daily limits use it, so they agree
# on when a day starts regardless of the server's or the session's time zone.
UTC_DAY_START = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"

# SQL for a user's tier: 'free', 'pro' or 'ultra'. A paid plan wins; otherwise a running free trial
# counts as 'pro' (the middle tier); otherwise free. Written against the users table or an alias of it,
# so a column prefix can be added with .replace if needed. The one definition of what a user gets.
TIER = "(CASE WHEN plan <> 'free' THEN plan WHEN trial_ends_at > now() THEN 'pro' ELSE 'free' END)"

# SQL for a user's daily limit: the :ultra, :pro or :free value for their tier.
# Every query that uses it binds :uid, :free, :pro and :ultra.
PLAN_LIMIT = f"(CASE COALESCE((SELECT {TIER} FROM users WHERE id = :uid), 'free') WHEN 'ultra' THEN :ultra WHEN 'pro' THEN :pro ELSE :free END)"


def tier_params(free, pro, ultra) -> dict:
    """The :free/:pro/:ultra bindings PLAN_LIMIT needs, so call sites can't forget one."""
    return {"free": free, "pro": pro, "ultra": ultra}


async def spend(engine: AsyncEngine, user_id: str, kind: str, free: int, pro: int, ultra: int) -> bool:
    """Record one use of `kind` today, unless the user's tier is at its daily limit (False).

    Uses are counted in usage_events, a ledger that nothing else deletes. They used to be counted from the
    stored files, messages, runs and quizzes, so 'Clear all data' or deleting a file reset the limit.
    Counting and recording are one statement, so there is no gap between the check and the insert."""
    async with engine.begin() as conn:
        recorded = (await conn.execute(text(
            "INSERT INTO usage_events (user_id, kind) SELECT :uid, :kind WHERE ("
            f"  SELECT count(*) FROM usage_events WHERE user_id = :uid AND kind = :kind AND created_at >= {UTC_DAY_START}"
            f") < {PLAN_LIMIT} RETURNING id"
        ), {"uid": user_id, "kind": kind, **tier_params(free, pro, ultra)})).scalar_one_or_none()
    return recorded is not None


async def tier_of(engine: AsyncEngine, user_id: str) -> str:
    """'free', 'pro' or 'ultra' (a running free trial counts as 'pro')."""
    async with engine.connect() as conn:
        tier = (await conn.execute(text(f"SELECT {TIER} FROM users WHERE id = :uid"), {"uid": user_id})).scalar_one_or_none()
    return tier or "free"


async def is_pro(engine: AsyncEngine, user_id: str) -> bool:
    """Paid-tier access: Pro or Ultra, or a running free trial. The gate for Talk, Agent and web search."""
    return await tier_of(engine, user_id) != "free"


def trial_state(plan: str, trial_ends_at: datetime | None, now: datetime) -> dict:
    """What the UI needs about the free trial. available = never started and not already paying."""
    return {
        "active": trial_ends_at is not None and trial_ends_at > now,
        "ends_at": trial_ends_at.isoformat() if trial_ends_at else None,
        "available": trial_ends_at is None and plan == "free",
    }


async def require_pro(engine: AsyncEngine, user_id: str, feature: str) -> None:
    """Refuse Free users with a plain message, before any work or quota is spent on their request."""
    if not await is_pro(engine, user_id):
        raise HTTPException(status_code=403, detail=f"{feature} is part of Pro")


def make_engine() -> AsyncEngine:
    # prepare_threshold=None: Neon's pooler (PgBouncer, transaction mode) breaks server-side prepared statements.
    connect_args = {"prepare_threshold": None}
    return create_async_engine(settings.database_url, echo=settings.db_echo, pool_pre_ping=True, connect_args=connect_args)
