import asyncio
import sys

from google import genai
from google.genai import types

from recall.config import settings
from recall.embeddings import make_genai_client


async def transcribe(client: genai.Client, data: bytes, mime_type: str) -> str:
    resp = await client.aio.models.generate_content(
        model=settings.transcribe_model,
        contents=[types.Part.from_bytes(data=data, mime_type=mime_type)]
    )
    return "" if resp.text is None else resp.text.strip()


if __name__ == "__main__":
    path, mime = sys.argv[1], sys.argv[2]
    with open(path, "rb") as f:
        audio = f.read()
    text = asyncio.run(transcribe(make_genai_client(), audio, mime))
    print(f"{len(text)} chars")
    print(text)