import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import UUID

import google.auth
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.cloud import storage
from sqlalchemy import text

from .config import settings
from .db import make_engine
from .extract import extract_text
from .chunking import chunk_pages
from .embeddings import embed, make_genai_client


# uvicorn only configures its own loggers; without this, INFO from this module is dropped.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = make_engine()
    credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    app.state.bucket = storage.Client(credentials=credentials, project=project).bucket(settings.gcs_bucket)
    app.state.genai = make_genai_client()
    yield
    await app.state.engine.dispose()


app = FastAPI(lifespan=lifespan)


@app.post("/")
async def handle_event(request: Request):
    event = await request.json()
    name = event.get("name", "")
    file_id = file_id_from_object_name(name)
    if file_id is None:
        logger.info("Ignoring object %s", name)
        return {"status": "ignored"}

    engine = request.app.state.engine
    # Atomic claim: only one delivery of a (possibly duplicated) event gets the row. A file left
    # 'processing' past the lease was abandoned by a crashed worker, so it can be claimed again.
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE files SET status = 'processing', claimed_at = now(), attempts = attempts + 1 "
                "WHERE id = :id AND (status = 'pending' OR (status = 'processing' "
                "AND claimed_at < now() - make_interval(secs => :lease))) "
                "RETURNING gcs_path, mime_type, user_id, attempts, kind"
            ),
            {"id": file_id, "lease": settings.claim_lease_seconds},
        )
        row = result.first()
        if row is None:
            current = (await conn.execute(text("SELECT status FROM files WHERE id = :id"),
                                          {"id": file_id})).scalar()
        elif row.attempts > settings.max_claim_attempts:
            await conn.execute(text("UPDATE files SET status = 'failed', error = :error WHERE id = :id"),
                               {"error": f"Gave up after {settings.max_claim_attempts} attempts", "id": file_id})
    if row is None:
        code, body = unclaimed_response(current)
        logger.info("Not claiming %s (status %s): %d %s", file_id, current, code, body["status"])
        return JSONResponse(body, status_code=code)
    if row.attempts > settings.max_claim_attempts:
        logger.warning("Giving up on %s after %d attempts", file_id, settings.max_claim_attempts)
        return {"status": "failed"}

    # Any failure must land in 'failed', or the row stays stuck in 'processing'.
    try:
        data = await asyncio.to_thread(request.app.state.bucket.blob(row.gcs_path).download_as_bytes)
        pages = await asyncio.to_thread(extract_text, data, row.mime_type)
        chunks = chunk_pages(pages)
        if not chunks:
            raise ValueError("No extractable text")
        vectors = await embed(request.app.state.genai, [content for _, content in chunks])
        async with engine.begin() as conn:  # chunks + 'ready' commit together or not at all
            await conn.execute(
                text("INSERT INTO chunks (file_id, user_id, chunk_no, page, content, embedding) "
                     "VALUES (:file_id, :user_id, :chunk_no, :page, :content, CAST(:embedding AS halfvec))"),
                [{"file_id": file_id, "user_id": row.user_id, "chunk_no": i, "page": page,
                  "content": content, "embedding": str(vec)}
                 for i, ((page, content), vec) in enumerate(zip(chunks, vectors))],
            )
            await conn.execute(text("UPDATE files SET status = 'ready', error = NULL WHERE id = :id"),
                               {"id": file_id})
        logger.info("Indexed %s: %d pages, %d chunks", file_id, len(pages), len(chunks))
        status = "ready"
    except Exception as e:
        logger.exception("Processing failed for %s", file_id)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE files SET status = 'failed', error = :error WHERE id = :id"),
                               {"error": str(e), "id": file_id})
        status = "failed"
    return {"status": status}


def unclaimed_response(status: str | None) -> tuple[int, dict]:
    """HTTP reply for an event whose file could not be claimed.

    Eventarc treats 2xx as handled and never redelivers, so a file still 'processing' under a
    live lease gets a non-2xx: Eventarc retries with backoff, and a later retry reclaims the
    file if its claim expires (the worker crashed). Finished or unknown files are acknowledged.
    """
    if status == "processing":
        return 409, {"status": "in_progress"}
    return 200, {"status": "skipped"}


def file_id_from_object_name(name: str) -> UUID | None:
    parts = name.split("/")
    if len(parts) != 4 or parts[0] != "users" or parts[2] not in ("docs","audio"):
        return None
    stem = parts[3].rsplit(".", 1)[0]
    try:
        return UUID(stem)
    except ValueError:
        return None
