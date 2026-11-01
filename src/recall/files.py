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
