import io
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from recall import auth, speech


def test_tts_returns_playable_audio_and_enforces_limits(monkeypatch):
    app = FastAPI()
    app.include_router(speech.router, prefix="/api/v1")
    app.dependency_overrides[auth.get_current_user_id] = lambda: "student"
    audio = SimpleNamespace(data=b"\0\0" * 20, mime_type="audio/L16;rate=24000")
    response = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=audio)]))])
    generate = AsyncMock(return_value=response)
    app.state.genai = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    meter = AsyncMock()
    monkeypatch.setattr(speech, "meter", meter)
    client = TestClient(app)

    result = client.post("/api/v1/speech/tts", json={"text": "Hello"})
    assert result.status_code == 200
    assert result.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(result.content)) as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getnframes()) == (1, 24000, 20)
    meter.assert_awaited_once()
    assert client.post("/api/v1/speech/tts", json={"text": ""}).status_code == 422
    assert generate.await_count == 1


def test_stream_tts_yields_all_pcm_and_checks_empty_output(monkeypatch):
    app = FastAPI()
    app.include_router(speech.router, prefix="/api/v1")
    app.dependency_overrides[auth.get_current_user_id] = lambda: "student"
    monkeypatch.setattr(speech, "meter", AsyncMock())
    audio = lambda data: SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(
        parts=[SimpleNamespace(inline_data=SimpleNamespace(data=data, mime_type="audio/L16;rate=24000"))]))])

    async def produce():
        yield audio(b"\x01\x02\x03")
        yield audio(b"\x04\x05\x06")

    generate = AsyncMock(return_value=produce())
    app.state.genai = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate)))
    client = TestClient(app)
    result = client.post("/api/v1/speech/tts/stream", json={"text": "Hello"})
    assert result.status_code == 200
    assert result.content == b"\x01\x02\x03\x04\x05\x06"
    assert result.headers["content-type"] == "application/octet-stream"
    generate.assert_awaited_once()

    async def empty():
        if False:
            yield None

    generate.return_value = empty()
    result = client.post("/api/v1/speech/tts/stream", json={"text": "Hello"})
    assert result.status_code == 502
    assert result.json()["detail"] == "Could not generate speech"
