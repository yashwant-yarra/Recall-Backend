import io
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from docx import Document
from pypdf import PdfReader

OFFICE_TYPES = {
    "application/msword": "doc",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


def office_to_pdf(data: bytes, mime_type: str) -> bytes:
    """Convert Office bytes to PDF, preserving page/slide order and visual layout."""
    with TemporaryDirectory(prefix="recall-office-") as directory:
        folder = Path(directory)
        source = folder / f"document.{OFFICE_TYPES[mime_type]}"
        source.write_bytes(data)
        # Include hidden slides so citation numbers still match the original presentation.
        output_format = ("pdf" if mime_type == "application/msword" else
                         'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"}}')
        # Separate profiles prevent concurrent conversions from sharing a LibreOffice process.
        try:
            subprocess.run(
                ["libreoffice", f"-env:UserInstallation={(folder / 'profile').as_uri()}",
                 "--headless", "--norestore", "--convert-to", output_format, "--outdir", str(folder), str(source)],
                check=True, capture_output=True, timeout=60,
            )
        except FileNotFoundError as e:
            raise ValueError("Office conversion requires LibreOffice Writer and Impress installed on the worker") from e
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise ValueError("Office conversion failed. Try an unencrypted file or export it as PDF.") from e
        converted = folder / "document.pdf"
        if not converted.is_file():
            raise ValueError("Cannot read this Office file. Try an unencrypted file or export it as PDF.")
        return converted.read_bytes()


def extract_text(data: bytes, mime_type: str) -> list[str]:
    """Extract text from a document based on its MIME type.

    Args:
        data: The document contents as bytes.
        mime_type: The document's MIME type.

    Returns:
        Extracted text, split into pages/slides for PDF, DOC and PowerPoint,
        and returned as one item for DOCX and plain-text files.

    Raises:
        ValueError: If the MIME type is not supported.
    """
    if mime_type in OFFICE_TYPES:
        return extract_text(office_to_pdf(data, mime_type), "application/pdf")
    if mime_type == "application/pdf":
        reader = PdfReader(io.BytesIO(data))
        return [page.extract_text() or "" for page in reader.pages]
    elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        doc = Document(io.BytesIO(data))
        return ["\n".join(p.text for p in doc.paragraphs)]
    elif mime_type == "text/plain":
        return [data.decode("utf-8-sig", errors="replace")]  # -sig drops a leading BOM if present
    raise ValueError(f"Unsupported mime type: {mime_type}")
