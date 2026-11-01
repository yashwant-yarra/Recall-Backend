import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, call
from uuid import uuid4

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import PreconditionFailed

from recall import files

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.mark.parametrize("cached", [True, False])
def test_presentation_preview_caches_pdf(monkeypatch, cached):
    bucket = MagicMock()
    preview, original = MagicMock(), MagicMock()
    preview.exists.return_value = cached
    original.download_as_bytes.return_value = b"slides"
    bucket.blob.side_effect = lambda path: preview if path.endswith(".preview.pdf") else original
    convert = MagicMock(return_value=b"%PDF-slides")
    monkeypatch.setattr(files, "office_to_pdf", convert)
    assert files.presentation_preview(bucket, "users/me/docs/deck.pptx", PPTX) == "users/me/docs/deck.pptx.preview.pdf"
    if cached:
        convert.assert_not_called()
        original.download_as_bytes.assert_not_called()
    else:
        convert.assert_called_once_with(b"slides", PPTX)
        preview.upload_from_string.assert_called_once_with(b"%PDF-slides", content_type="application/pdf", if_generation_match=0)


def test_simultaneous_preview_creation_is_safe(monkeypatch):
    bucket = MagicMock()
    bucket.blob.return_value.exists.return_value = False
    bucket.blob.return_value.upload_from_string.side_effect = PreconditionFailed("Already created")
    monkeypatch.setattr(files, "office_to_pdf", lambda *args: b"%PDF-slides")
    assert files.presentation_preview(bucket, "deck.pptx", PPTX) == "deck.pptx.preview.pdf"


@pytest.mark.parametrize("mime", [PPTX, "application/vnd.ms-powerpoint", "application/pdf"])
def test_file_url_returns_browser_viewable_pdf(monkeypatch, mime):
    file_id = uuid4()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gcs_bucket=object(), gcs_credentials=object())))
    row = SimpleNamespace(gcs_path="deck.pptx", mime_type=mime, display_name="Lecture.pptx", kind="doc", status="ready")
    owned = AsyncMock(return_value=row)
    preview = MagicMock(return_value="deck.pptx.preview.pdf")
    sign = MagicMock(return_value="https://storage.example/slides.pdf")
    monkeypatch.setattr(files, "_owned_file", owned)
    monkeypatch.setattr(files, "presentation_preview", preview)
    monkeypatch.setattr(files, "sign_download_url", sign)
    result = asyncio.run(files.file_url(file_id, request, "me"))
    owned.assert_awaited_once_with(request, "me", file_id)
    assert result["mime_type"] == "application/pdf"
    assert result["name"] == "Lecture.pptx"
    if mime == "application/pdf":
        preview.assert_not_called()
    else:
        sign.assert_called_once_with(ANY, ANY, "deck.pptx.preview.pdf", "application/pdf", "Lecture.pdf")


def test_failed_preview_returns_readable_error(monkeypatch):
    row = SimpleNamespace(gcs_path="deck.pptx", mime_type=PPTX, display_name=None, status="ready")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gcs_bucket=object())))
    monkeypatch.setattr(files, "_owned_file", AsyncMock(return_value=row))
    monkeypatch.setattr(files, "presentation_preview", MagicMock(side_effect=ValueError("Cannot convert this file")))
    with pytest.raises(HTTPException) as error:
        asyncio.run(files.file_url(uuid4(), request, "me"))
    assert error.value.status_code == 422
    assert error.value.detail == "Cannot convert this file"


def test_deleting_file_removes_cached_preview(monkeypatch):
    engine = MagicMock()
    conn = engine.begin.return_value.__aenter__.return_value
    conn.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: "deck.pptx"))
    bucket = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine, gcs_bucket=bucket)))
    delete = MagicMock()
    monkeypatch.setattr(files, "delete_object", delete)
    asyncio.run(files.delete_file(uuid4(), request, "me"))
    assert delete.call_args_list == [call(bucket, "deck.pptx"), call(bucket, "deck.pptx.preview.pdf")]
