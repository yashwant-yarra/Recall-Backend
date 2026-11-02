"""Voice in and out: a spoken question becomes text, and an answer can be read aloud.

POST /speech/transcribe takes the recorded audio as the raw request body and returns its text
(Gemini Transcribe detects the language). POST /speech/tts turns a short piece of text into a WAV
clip; the UI sends an answer one piece at a time, so it can start playing after the first.
"""
import io
import re
import wave

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from google.genai import types
from pydantic import BaseModel, Field
from sqlalchemy import text

from recall import auth
from recall.config import settings
from recall.db import spend
from recall.transcribe import transcribe

router = APIRouter()

VOICE_INPUT_TYPES = {"audio/webm", "audio/mp4", "audio/ogg", "audio/wav", "audio/mpeg"}


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=settings.max_tts_chars)


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    """Gemini TTS returns raw 16-bit mono PCM; browsers play it once it has a WAV header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def sample_rate(mime_type: str | None, default: int = 24000) -> int:
    """'audio/L16;codec=pcm;rate=24000' -> 24000."""
    m = re.search(r"rate=(\d+)", mime_type or "")
    return int(m.group(1)) if m else default


async def meter(request: Request, user_id: str, kind: str, free: int, pro: int, ultra: int) -> None:
    """Record one use, unless this week's limit for the user's plan is reached (429)."""
    if not await spend(request.app.state.engine, user_id, kind, free, pro, ultra):
        raise HTTPException(status_code=429, detail="Weekly voice limit reached for your plan")


@router.post("/speech/transcribe")
async def transcribe_question(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    mime = (request.headers.get("content-type") or "").split(";")[0].strip()
    if mime not in VOICE_INPUT_TYPES:
        raise HTTPException(status_code=415, detail="Send the recording as audio (webm, mp4, ogg, wav or mp3)")
    if int(request.headers.get("content-length") or 0) > settings.max_voice_input_bytes:
        raise HTTPException(status_code=413, detail="Recording too long; keep it under about 2 minutes")
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=422, detail="Empty recording")
    if len(audio) > settings.max_voice_input_bytes:  # the header can be missing or wrong
        raise HTTPException(status_code=413, detail="Recording too long; keep it under about 2 minutes")
    await meter(request, user_id, "transcribe", settings.daily_voice_inputs, settings.pro_daily_voice_inputs, settings.ultra_daily_voice_inputs)
    return {"text": await transcribe(request.app.state.genai, audio, mime)}


@router.post("/speech/tts")
async def speak(body: SpeakRequest, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    await meter(request, user_id, "tts", settings.daily_tts_requests, settings.pro_daily_tts_requests, settings.ultra_daily_tts_requests)
    resp = await request.app.state.genai.aio.models.generate_content(
        model=settings.tts_model,
        contents=body.text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.tts_voice))),
        ),
    )
    parts = resp.candidates[0].content.parts if resp.candidates and resp.candidates[0].content else []
    audio = next((p.inline_data for p in parts or [] if p.inline_data and p.inline_data.data), None)
    if audio is None:
        raise HTTPException(status_code=502, detail="Could not generate speech")
    # The text decides the language: Gemini TTS speaks Hindi text in Hindi, Tamil in Tamil, and so on.
    wav = pcm_to_wav(audio.data, sample_rate(audio.mime_type))
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/speech/tts/stream")
async def speak_stream(body: SpeakRequest, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    await meter(request, user_id, "tts", settings.daily_tts_requests, settings.pro_daily_tts_requests, settings.ultra_daily_tts_requests)
    stream = await request.app.state.genai.aio.models.generate_content_stream(
        model=settings.tts_model, contents=body.text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.tts_voice))),
        ),
    )

    async def chunks():
        async for response in stream:
            for candidate in response.candidates or []:
                for part in candidate.content.parts if candidate.content and candidate.content.parts else []:
                    if part.inline_data and part.inline_data.data:
                        if sample_rate(part.inline_data.mime_type) != 24000:
                            raise ValueError("Unexpected speech sample rate")
                        yield part.inline_data.data

    audio = chunks()
    first = await anext(audio, None)
    if not first:
        await audio.aclose()
        if hasattr(stream, "aclose"):
            await stream.aclose()
        raise HTTPException(status_code=502, detail="Could not generate speech")

    async def output():
        try:
            yield first
            async for chunk in audio:
                yield chunk
        finally:
            await audio.aclose()
            if hasattr(stream, "aclose"):
                await stream.aclose()

    return StreamingResponse(output(), media_type="application/octet-stream", headers={"Cache-Control": "no-store"})
