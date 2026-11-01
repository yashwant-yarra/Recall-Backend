"""Mark rows that can never finish on their own as failed.

Run by hand:  uv run python -m recall.cleanup            (applies the changes)
              uv run python -m recall.cleanup --dry-run  (counts only, rolls back)
"""
import asyncio
import selectors
import sys

from sqlalchemy import text

from recall.db import make_engine

# Uploads: the client got a signed URL (15 min TTL) but never uploaded, so no event will ever come.
# A day is far past the URL's expiry.
STALE_UPLOADS = text(
    "UPDATE files SET status = 'failed', error = 'Upload never completed' "
    "WHERE status = 'pending' AND created_at < now() - interval '1 day'"
)
# Agent runs: a live run lasts at most one request (Cloud Run's timeout is 300 s), so a run still
# 'running' after an hour lost its client mid-stream. Runs awaiting approval are left alone:
# the user may still answer.
STALE_RUNS = text(
    "UPDATE agent_runs SET status = 'failed', error = 'Abandoned: the client disconnected', "
    "finished_at = now() WHERE status = 'running' AND created_at < now() - interval '1 hour'"
)


async def cleanup(engine, dry_run: bool = False) -> dict:
    """Returns how many rows each rule matched. With dry_run, the changes are rolled back."""
    async with engine.connect() as conn:
        async with conn.begin() as tx:
            uploads = (await conn.execute(STALE_UPLOADS)).rowcount
            runs = (await conn.execute(STALE_RUNS)).rowcount
            if dry_run:
                await tx.rollback()
    return {"stale_uploads": uploads, "stale_runs": runs, "dry_run": dry_run}


async def main() -> None:
    engine = make_engine()
    print(await cleanup(engine, dry_run="--dry-run" in sys.argv))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
