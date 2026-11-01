from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from .config import settings

# SQL for the start of the current UTC day, as a timestamptz. All daily limits use it, so they agree
# on when a day starts regardless of the server's or the session's time zone.
UTC_DAY_START = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"

# SQL for a user's daily limit: the :pro value if their plan is 'pro', else :free.
# Every query that uses it binds :uid, :free and :pro.
PLAN_LIMIT = "(CASE WHEN (SELECT plan FROM users WHERE id = :uid) = 'pro' THEN :pro ELSE :free END)"

async def is_pro(engine: AsyncEngine, user_id: str) -> bool:
    async with engine.connect() as conn:
        plan = (await conn.execute(text("SELECT plan FROM users WHERE id = :uid"), {"uid": user_id})).scalar_one_or_none()
    return plan == "pro"


async def require_pro(engine: AsyncEngine, user_id: str, feature: str) -> None:
    """Refuse Free users with a plain message, before any work or quota is spent on their request."""
    if not await is_pro(engine, user_id):
        raise HTTPException(status_code=403, detail=f"{feature} is part of Pro")


def make_engine() -> AsyncEngine:
    # prepare_threshold=None: Neon's pooler (PgBouncer, transaction mode) breaks server-side prepared statements.
    connect_args = {"prepare_threshold": None}
    return create_async_engine(settings.database_url, echo=settings.db_echo, pool_pre_ping=True, connect_args=connect_args)