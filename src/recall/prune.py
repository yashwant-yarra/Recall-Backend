"""Delete the oldest files of users who stayed over the Free limits long after their pass or trial ended.

When a pass (or the free trial) ends, nothing is deleted: the files stay and only new uploads are refused.
After storage_grace_days (90) on Free while still over the Free file or storage limit, this job deletes
their oldest files until what's left fits the Free limits (the newest files that fit are kept). The UI
shows the date in advance (GET /me storage.delete_after).

Run by hand (lists only; nothing is deleted without --apply):
    uv run python -m recall.prune            (who and what would be deleted)
    uv run python -m recall.prune --apply    (deletes)
Needs storage delete rights: run with the recall-api credentials (see CLAUDE.md, local credentials).
"""
import asyncio
import selectors
import sys
from datetime import datetime, timedelta

import google.auth
from google.cloud import storage
from sqlalchemy import text

from recall.config import settings
from recall.db import TIER, make_engine
from recall.files import object_paths
from recall.storage import delete_object


def deletion_date(paid_until: datetime | None, trial_ends_at: datetime | None, grace_days: int) -> datetime | None:
    """When files over the Free limits may be deleted: grace_days after paid access last ended (the later of
    the pass and the trial). None if the user never had paid access, so was never over a paid limit."""
    ended = max((t for t in (paid_until, trial_ends_at) if t is not None), default=None)
    return ended + timedelta(days=grace_days) if ended else None


def storage_state(on_free: bool, used: int, files: int, paid_until: datetime | None, trial_ends_at: datetime | None,
                  max_files: int, max_bytes: int, grace_days: int) -> dict:
    """What the UI shows about storage. over_free_limit and delete_after only apply on Free (no running pass
    or trial); delete_after is when this job may remove the oldest files (ISO, or None)."""
    over = on_free and (files > max_files or used > max_bytes)
    when = deletion_date(paid_until, trial_ends_at, grace_days) if over else None
    return {"used_bytes": used, "files": files, "over_free_limit": over,
            "delete_after": when.isoformat() if when else None}


def files_to_delete(files: list[tuple], max_files: int, max_bytes: int) -> list:
    """files: (id, size_bytes, created_at). Keeps the newest files that fit within the limits (a file too big
    for what's left is skipped, and older smaller ones can still fit); returns the ids of the rest."""
    kept, used, doomed = 0, 0, []
    for file_id, size, _ in sorted(files, key=lambda f: f[2], reverse=True):
        if kept < max_files and used + size <= max_bytes:
            kept, used = kept + 1, used + size
        else:
            doomed.append(file_id)
    return doomed


# Users on Free (no running pass or trial) whose grace period is over and who are over a Free limit.
OVER_LIMIT_USERS = text(f"""
    SELECT id FROM users
    WHERE {TIER} = 'free'
      AND GREATEST(paid_until, trial_ends_at) < now() - make_interval(days => :grace)
      AND (SELECT count(*) > :max_files OR COALESCE(sum(f.size_bytes), 0) > :max_bytes
           FROM files f WHERE f.user_id = users.id)
""")
# Delete only while the user is still on Free: someone who buys a pass mid-run keeps everything.
DELETE_FILES = text(f"""
    DELETE FROM files WHERE id = ANY(:ids) AND user_id = :uid
      AND (SELECT {TIER} FROM users WHERE id = :uid) = 'free'
    RETURNING gcs_path
""")


async def prune(engine, bucket, apply: bool) -> dict:
    limits = {"grace": settings.storage_grace_days, "max_files": settings.free_max_files,
              "max_bytes": settings.free_max_storage}
    async with engine.connect() as conn:
        users = (await conn.execute(OVER_LIMIT_USERS, limits)).scalars().all()
    report = {"apply": apply, "users": len(users), "files": 0, "bytes": 0}
    for uid in users:
        async with engine.connect() as conn:
            files = (await conn.execute(text(
                "SELECT id, size_bytes, created_at FROM files WHERE user_id = :uid"), {"uid": uid})).all()
        doomed = files_to_delete([tuple(f) for f in files], settings.free_max_files, settings.free_max_storage)
        size = sum(f.size_bytes for f in files if f.id in doomed)
        print(f"user {uid}: {len(doomed)} of {len(files)} files, {size // (1024 * 1024)} MB{' deleted' if apply else ' would be deleted'}")
        report["files"] += len(doomed)
        report["bytes"] += size
        if not apply or not doomed:
            continue
        # Rows first (as in files.delete_file), so the files are gone for the user even if storage fails;
        # a leftover object is harmless (nothing points to it, and the bucket's soft delete expires it).
        async with engine.begin() as conn:
            paths = (await conn.execute(DELETE_FILES, {"ids": doomed, "uid": uid})).scalars().all()
        for gcs_path in paths:
            for object_path in object_paths(gcs_path):
                try:
                    await asyncio.to_thread(delete_object, bucket, object_path)
                except Exception as e:  # keep going: the rows are already gone
                    print(f"  could not remove {object_path}: {e}")
    return report


async def main() -> None:
    credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    bucket = storage.Client(credentials=credentials, project=project).bucket(settings.gcs_bucket)
    engine = make_engine()
    print(await prune(engine, bucket, apply="--apply" in sys.argv))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
