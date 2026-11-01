import asyncio
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from recall import auth
from recall.db import PLAN_LIMIT, UTC_DAY_START
from recall.storage import sign_upload_url

from .config import settings

# Allow-list: mime type -> (kind, extension). Anything not listed is rejected.
ALLOWED_TYPES = {
    "application/pdf": ("doc", "pdf"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("doc", "docx"),
    "text/plain": ("doc", "txt"),
    "audio/webm": ("audio", "webm"),
    "audio/mpeg": ("audio", "mp3"),
    "audio/mp4": ("audio", "m4a"),
    "audio/wav": ("audio", "wav"),
}


class CreateFileRequest(BaseModel):
    mime_type: str
    size_bytes: int = Field(gt=0, le=settings.max_upload_size)


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
    file_id = uuid4()
    folder = "audio" if kind == "audio" else "docs"
    gcs_path = f"users/{user_id}/{folder}/{file_id}.{ext}"
    async with request.app.state.engine.begin() as conn:
        # Insert only while the user is under today's upload limit (count + insert in one statement).
        inserted = (await conn.execute(
            text("INSERT INTO files (id, user_id, kind, gcs_path, mime_type, size_bytes) "
                 "SELECT :id, :uid, :kind, :gcs_path, :mime_type, :size_bytes WHERE ("
                 f"  SELECT count(*) FROM files WHERE user_id = :uid AND created_at >= {UTC_DAY_START}"
                 f") < {PLAN_LIMIT} RETURNING id"),
            {"id": str(file_id), "uid": user_id, "kind": kind, "gcs_path": gcs_path, "mime_type": body.mime_type,
             "size_bytes": body.size_bytes, "free": settings.daily_uploads, "pro": settings.pro_daily_uploads}
        )).scalar_one_or_none()
    if inserted is None:
        raise HTTPException(status_code=429, detail="Daily upload limit reached for your plan")
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
            "SELECT f.id, f.kind, f.mime_type, f.size_bytes, f.status, f.error, f.created_at, "
            "v.summary, v.action_items, v.topics, "
            "(SELECT left(c.content, 160) FROM chunks c WHERE c.file_id = f.id ORDER BY c.chunk_no LIMIT 1) AS preview "
            "FROM files f LEFT JOIN voice_notes v ON v.file_id = f.id "
            "WHERE f.user_id = :uid ORDER BY f.created_at DESC LIMIT 50"
        ), {"uid": user_id})).all()
    return {"files": [{
        "file_id": str(r.id), "kind": r.kind, "mime_type": r.mime_type, "size_bytes": r.size_bytes,
        "status": r.status, "error": r.error, "created_at": r.created_at.isoformat(),
        "preview": r.preview, "summary": r.summary,
        "action_items": r.action_items or [], "topics": r.topics or [],
    } for r in rows]}
