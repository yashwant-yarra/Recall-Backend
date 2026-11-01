from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from .config import settings

# SQL for the start of the current UTC day, as a timestamptz. All daily limits use it, so they agree
# on when a day starts regardless of the server's or the session's time zone.
UTC_DAY_START = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"

# SQL for a user's daily limit: the :pro value if their plan is 'pro', else :free.
# Every query that uses it binds :uid, :free and :pro.
PLAN_LIMIT = "(CASE WHEN (SELECT plan FROM users WHERE id = :uid) = 'pro' THEN :pro ELSE :free END)"

def make_engine() -> AsyncEngine:
    # prepare_threshold=None: Neon's pooler (PgBouncer, transaction mode) breaks server-side prepared statements.
    connect_args = {"prepare_threshold": None}
    return create_async_engine(settings.database_url, echo=settings.db_echo, pool_pre_ping=True, connect_args=connect_args)