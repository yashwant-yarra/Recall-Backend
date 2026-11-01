import io
from docx import Document
from pypdf import PdfReader

def extract_text(data: bytes, mime_type: str) -> list[str]:
    """Extract text from a document based on its MIME type.

    Args:
        data: The document contents as bytes.
        mime_type: The document's MIME type.

    Returns:
        Extracted text, split into pages for PDFs and returned as one item
        for DOCX and plain-text files.

    Raises:
        ValueError: If the MIME type is not supported.
    """
    if mime_type == "application/pdf":
        reader = PdfReader(io.BytesIO(data))
        return [page.extract_text() or "" for page in reader.pages]
    elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        doc = Document(io.BytesIO(data))
        return ["\n".join(p.text for p in doc.paragraphs)]
    elif mime_type == "text/plain":
        return [data.decode("utf-8-sig", errors="replace")]  # -sig drops a leading BOM if present
    raise ValueError(f"Unsupported mime type: {mime_type}")