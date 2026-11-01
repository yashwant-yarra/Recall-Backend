import io
import subprocess
from pathlib import Path
import pytest
from docx import Document
from recall.extract import extract_text
from recall.extract import OFFICE_TYPES

def test_txt():
    assert extract_text("héllo".encode(), "text/plain") == ["héllo"]
def test_txt_bom_stripped():
    # Notepad-style UTF-8 BOM: left in, it glues to the first word and breaks full-text search on it.
    assert extract_text(b"\xef\xbb\xbfhello", "text/plain") == ["hello"]
def test_docx():
    doc = Document()
    doc.add_paragraph("first")
    doc.add_paragraph("second")
    buf = io.BytesIO()
    doc.save(buf)
    assert extract_text(buf.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document") == ["first\nsecond"]

def test_unsupported():
    with pytest.raises(ValueError):
        extract_text(b"x", "image/png")


@pytest.mark.parametrize("mime,extension", OFFICE_TYPES.items())
def test_office_conversion_preserves_pages_and_cleans_up(monkeypatch, mime, extension):
    from pypdf import PdfWriter
    from recall.files import ALLOWED_TYPES

    assert ALLOWED_TYPES[mime] == ("doc", extension)
    folders = []

    def convert(command, **options):
        source = Path(command[-1])
        folders.append(source.parent)
        assert source.suffix == f".{extension}"
        assert source.read_bytes() == b"office bytes"
        assert "--headless" in command
        assert options == {"check": True, "capture_output": True, "timeout": 60}
        pdf = PdfWriter()
        pdf.add_blank_page(width=100, height=100)
        pdf.add_blank_page(width=100, height=100)
        pdf.write(source.with_suffix(".pdf"))

    monkeypatch.setattr(subprocess, "run", convert)
    assert extract_text(b"office bytes", mime) == ["", ""]
    assert not folders[0].exists()


@pytest.mark.parametrize("error,message", [
    (FileNotFoundError(), "requires LibreOffice"),
    (subprocess.TimeoutExpired("libreoffice", 60), "conversion failed"),
    (subprocess.CalledProcessError(1, "libreoffice"), "conversion failed"),
])
def test_office_conversion_errors(monkeypatch, error, message):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError, match=message):
        extract_text(b"broken", "application/msword")


def test_office_conversion_missing_output(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="Cannot read this Office file"):
        extract_text(b"broken", "application/msword")
