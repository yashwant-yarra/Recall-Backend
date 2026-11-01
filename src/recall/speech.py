"""Voice in and out: a spoken question becomes text, and an answer can be read aloud.

POST /speech/transcribe takes the recorded audio as the raw request body and returns its text
(Gemini Transcribe detects the language). POST /speech/tts turns a short piece of text into a WAV
clip; the UI sends an answer one piece at a time, so it can start playing after the first.
"""
import io
import re
import wave

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from google.genai import types
from pydantic import BaseModel, Field
from sqlalchemy import text

from recall import auth
from recall.config import settings
from recall.db import PLAN_LIMIT, UTC_DAY_START
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


async def meter(request: Request, user_id: str, kind: str, free: int, pro: int) -> None:
    """Record one use, unless today's limit for the user's plan is reached (429).
    Counting and recording are one statement, so there's no gap between check and insert."""
    async with request.app.state.engine.begin() as conn:
        recorded = (await conn.execute(text(
            "INSERT INTO usage_events (user_id, kind) SELECT :uid, :kind WHERE ("
            f"  SELECT count(*) FROM usage_events WHERE user_id = :uid AND kind = :kind AND created_at >= {UTC_DAY_START}"
            f") < {PLAN_LIMIT} RETURNING id"
        ), {"uid": user_id, "kind": kind, "free": free, "pro": pro})).scalar_one_or_none()
    if recorded is None:
        raise HTTPException(status_code=429, detail="Daily voice limit reached for your plan")


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
    await meter(request, user_id, "transcribe", settings.daily_voice_inputs, settings.pro_daily_voice_inputs)
    return {"text": await transcribe(request.app.state.genai, audio, mime)}


@router.post("/speech/tts")
async def speak(body: SpeakRequest, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    await meter(request, user_id, "tts", settings.daily_tts_requests, settings.pro_daily_tts_requests)
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
