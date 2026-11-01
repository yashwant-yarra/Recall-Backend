import asyncio
import logging
import re
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from google.api_core.exceptions import PreconditionFailed
from pydantic import BaseModel, Field
from sqlalchemy import text

from recall import auth
from recall.db import spend, tier_of
from recall.extract import office_to_pdf
from recall.storage import delete_object, delete_prefix, sign_download_url, sign_upload_url

from .config import settings

logger = logging.getLogger(__name__)
# LibreOffice is memory-heavy; allow one preview conversion per API instance.
_preview_conversion = asyncio.Semaphore(1)
PREVIEW_SUFFIX = ".preview-v2.pdf"  # invalidate previews rendered before Office-compatible fonts were installed

# Allow-list: mime type -> (kind, extension). Anything not listed is rejected.
ALLOWED_TYPES = {
    "application/pdf": ("doc", "pdf"),
    "application/msword": ("doc", "doc"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("doc", "docx"),
    "application/vnd.ms-powerpoint": ("doc", "ppt"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ("doc", "pptx"),
    "text/plain": ("doc", "txt"),
    "audio/webm": ("audio", "webm"),
    "audio/mpeg": ("audio", "mp3"),
    "audio/mp4": ("audio", "m4a"),
    "audio/wav": ("audio", "wav"),
}


class CreateFileRequest(BaseModel):
    mime_type: str
    size_bytes: int = Field(gt=0, le=settings.max_upload_size)
    display_name: str | None = Field(default=None, max_length=500)


def clean_display_name(name: str | None) -> str | None:
    """The client's file name, made safe to show: last path part only, no control characters,
    at most 200 characters. Only ever displayed; storage paths are built from the file_id."""
    if not name:
        return None
    name = re.split(r"[\\/]", name)[-1]  # browsers may send "C:\fakepath\notes.pdf"
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:200] or None


class CreateFileResponse(BaseModel):
    file_id: UUID
    upload_url: str
    upload_headers: dict[str, str]


router = APIRouter()

@router.post("/files", status_code=201) 
async def create_file(body: CreateFileRequest, request: Request, user_id: str = Depends(auth.get_current_user_id)) -> CreateFileResponse:
    """Creates a new file record and returns a signed upload URL."""
    if body.mime_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported mime type: {body.mime_type}")
    kind, ext = ALLOWED_TYPES[body.mime_type]
    tier = await tier_of(request.app.state.engine, user_id)
    max_size, max_files, upgrade = {
        "free": (settings.free_max_upload_size, settings.free_max_files, "Pro"),
        "pro": (settings.pro_max_upload_size, settings.pro_max_files, "Ultra"),
        "ultra": (settings.max_upload_size, None, None),  # no file-count cap
    }[tier]
    hint = f" Upgrade to {upgrade} for more." if upgrade else ""
    if body.size_bytes > max_size:
        raise HTTPException(status_code=400, detail=f"The {tier.capitalize()} plan allows files up to {max_size // (1024 * 1024)} MB.{hint}")
    if max_files is not None:
        async with request.app.state.engine.connect() as conn:
            kept = (await conn.execute(text("SELECT count(*) FROM files WHERE user_id = :uid"), {"uid": user_id})).scalar_one()
        # ponytail: count-then-insert, so two simultaneous uploads at the cap can both pass by one.
        if kept >= max_files:
            raise HTTPException(status_code=403, detail=f"The {tier.capitalize()} plan keeps up to {max_files} files. Delete one or upgrade.{'' if not upgrade else ' ' + upgrade + ' keeps more.'}")
    file_id = uuid4()
    folder = "audio" if kind == "audio" else "docs"
    gcs_path = f"users/{user_id}/{folder}/{file_id}.{ext}"
    # Today's upload limit is counted in usage_events (db.spend), so deleting files or clearing data does
    # not give the uploads back. Spent after the size and file-count checks, so a refused upload uses none.
    if not await spend(request.app.state.engine, user_id, "upload", settings.daily_uploads, settings.pro_daily_uploads,
                       settings.ultra_daily_uploads):
        raise HTTPException(status_code=429, detail="Daily upload limit reached for your plan")
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO files (id, user_id, kind, gcs_path, mime_type, size_bytes, display_name) "
                 "VALUES (:id, :uid, :kind, :gcs_path, :mime_type, :size_bytes, :display_name)"),
            {"id": str(file_id), "uid": user_id, "kind": kind, "gcs_path": gcs_path, "mime_type": body.mime_type,
             "size_bytes": body.size_bytes, "display_name": clean_display_name(body.display_name)})
    url, headers = await asyncio.to_thread(
        sign_upload_url,
        request.app.state.gcs_bucket,
        request.app.state.gcs_credentials,
        gcs_path,
        body.mime_type,
        body.size_bytes,
    )
    return CreateFileResponse(file_id=file_id, upload_url=url, upload_headers=headers)


@router.get("/files")
async def list_files(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """The user's 50 newest uploads with their processing status, for the UI's library.

    No filenames are stored (the client's name is never trusted), so each file shows a preview:
    the start of its first chunk, plus the summary and action items for voice notes.
    """
    async with request.app.state.engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT f.id, f.kind, f.mime_type, f.size_bytes, f.status, f.error, f.created_at, f.display_name, "
            "v.summary, v.action_items, v.topics, "
            "(SELECT left(c.content, 160) FROM chunks c WHERE c.file_id = f.id ORDER BY c.chunk_no LIMIT 1) AS preview "
            "FROM files f LEFT JOIN voice_notes v ON v.file_id = f.id "
            "WHERE f.user_id = :uid ORDER BY f.created_at DESC LIMIT 50"
        ), {"uid": user_id})).all()
    return {"files": [{
        "file_id": str(r.id), "name": r.display_name, "kind": r.kind, "mime_type": r.mime_type, "size_bytes": r.size_bytes,
        "status": r.status, "error": r.error, "created_at": r.created_at.isoformat(),
        "preview": r.preview, "summary": r.summary,
        "action_items": r.action_items or [], "topics": r.topics or [],
    } for r in rows]}


async def _owned_file(request: Request, user_id: str, file_id: UUID):
    """The user's file row, or 404 (someone else's file looks the same as a missing one)."""
    async with request.app.state.engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT id, gcs_path, mime_type, kind, status, display_name FROM files WHERE id = :id AND user_id = :uid"
        ), {"id": file_id, "uid": user_id})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return row


def presentation_preview(bucket, gcs_path: str, mime_type: str) -> str:
    """Cache a PDF alongside the immutable original, including for existing uploads."""
    preview_path = f"{gcs_path}{PREVIEW_SUFFIX}"
    preview = bucket.blob(preview_path)
    if not preview.exists():
        pdf = office_to_pdf(bucket.blob(gcs_path).download_as_bytes(), mime_type)
        try:
            preview.upload_from_string(pdf, content_type="application/pdf", if_generation_match=0)
        except PreconditionFailed:
            pass  # another preview request created the same PDF first
    return preview_path


@router.get("/files/{file_id}/url")
async def file_url(file_id: UUID, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """A 5-minute preview link; PowerPoint slides are rendered as a cached PDF."""
    row = await _owned_file(request, user_id, file_id)
    if row.status == "pending":
        raise HTTPException(status_code=409, detail="This file hasn't finished uploading")
    gcs_path, mime_type, filename = row.gcs_path, row.mime_type, row.display_name
    if mime_type in ("application/vnd.ms-powerpoint",
                     "application/vnd.openxmlformats-officedocument.presentationml.presentation"):
        try:
            async with _preview_conversion:
                gcs_path = await asyncio.to_thread(presentation_preview, request.app.state.gcs_bucket, gcs_path, mime_type)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        mime_type = "application/pdf"
        filename = f"{filename.rsplit('.', 1)[0]}.pdf" if filename else "slides.pdf"
    url = await asyncio.to_thread(
        sign_download_url, request.app.state.gcs_bucket, request.app.state.gcs_credentials,
        gcs_path, mime_type, filename,
    )
    return {"url": url, "mime_type": mime_type, "kind": row.kind, "name": row.display_name,
            "expires_in": settings.download_url_ttl_seconds}


@router.get("/files/{file_id}/text")
async def file_text(file_id: UUID, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """The text extracted from a file, page by page: an Office preview, or a voice note's transcript.

    Rebuilt from chunks, so overlapping parts between chunks of a page appear twice (ponytail:
    store the full text per page if exact previews matter).
    """
    await _owned_file(request, user_id, file_id)
    async with request.app.state.engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT page, string_agg(content, E'\n' ORDER BY chunk_no) AS text FROM chunks "
            "WHERE file_id = :id GROUP BY page ORDER BY page NULLS FIRST"
        ), {"id": file_id})).all()
    return {"pages": [{"page": r.page, "text": r.text} for r in rows]}


@router.delete("/data", status_code=204)
async def clear_data(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """Delete everything the user has stored: files (with their chunks and voice notes), chats and
    messages, agent runs and steps, tasks, drafts and quizzes, then their objects in the bucket.
    The account, plan and billing stay."""
    # Fixed list (never user input), so building the SQL with an f-string is safe.
    async with request.app.state.engine.begin() as conn:
        for table in ("agent_runs", "chats", "tasks", "drafts", "quizzes", "files"):
            await conn.execute(text(f"DELETE FROM {table} WHERE user_id = :uid"), {"uid": user_id})
    # Rows first (as in delete_file), so the data is gone for the user even if storage fails. The
    # trailing slash stops 'users/ab' from matching another user's 'users/abc/'.
    try:
        await asyncio.to_thread(delete_prefix, request.app.state.gcs_bucket, f"users/{user_id}/")
    except Exception:
        logger.exception("Cleared the rows for user %s, but removing their storage objects failed", user_id)


@router.delete("/files/{file_id}", status_code=204)
async def delete_file(file_id: UUID, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """Delete a file everywhere: its chunks and voice note go with the row (ON DELETE CASCADE);
    chats, quizzes and tasks that pointed to it are kept, unlinked (ON DELETE SET NULL)."""
    async with request.app.state.engine.begin() as conn:
        gcs_path = (await conn.execute(text(
            "DELETE FROM files WHERE id = :id AND user_id = :uid RETURNING gcs_path"
        ), {"id": file_id, "uid": user_id})).scalar_one_or_none()
    if gcs_path is None:
        raise HTTPException(status_code=404, detail="File not found")
    # The row goes first, so the file disappears for the user even if storage is briefly unreachable;
    # a leftover object is harmless (nothing points to it, and the bucket's soft delete expires it).
    for object_path in (gcs_path, f"{gcs_path}.preview.pdf", f"{gcs_path}{PREVIEW_SUFFIX}"):
        try:
            await asyncio.to_thread(delete_object, request.app.state.gcs_bucket, object_path)
        except Exception:
            logger.exception("Deleted file %s, but removing %s from storage failed", file_id, object_path)
