"""Reading scanned PDFs: when a PDF has no text layer, Gemini reads the page images instead."""
from google import genai
from google.genai import types
from pydantic import BaseModel

from recall.config import settings

# A page with a real text layer has far more than this; scans give 0, or a few stray characters.
MIN_CHARS_PER_PAGE = 40


class Pages(BaseModel):
    pages: list[str]


def needs_ocr(pages: list[str]) -> bool:
    """True when pypdf found (almost) no text: the PDF is probably scanned or photographed."""
    if not pages:
        return True
    return sum(len(p.strip()) for p in pages) / len(pages) < MIN_CHARS_PER_PAGE


async def ocr_pdf(client: genai.Client, data: bytes) -> list[str]:
    """One string per page, as Gemini reads it from the page images.

    ponytail: the PDF is sent inline, which suits typical scans; very large files would need
    Part.from_uri("gs://…") with the Vertex service agent allowed to read the bucket.
    """
    resp = await client.aio.models.generate_content(
        model=settings.ocr_model,
        contents=[
            types.Part.from_bytes(data=data, mime_type="application/pdf"),
            "Transcribe the text of every page of this document, in reading order. Return one entry per page, "
            "in page order, with the page's text exactly as written (keep headings, lists and formulas; "
            "write formulas in plain text or LaTeX). Use an empty string for a blank page. "
            "Do not summarize or add anything.",
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Pages, temperature=0),
    )
    if resp.parsed is None:
        raise ValueError("Could not read the scanned PDF")
    return resp.parsed.pages
