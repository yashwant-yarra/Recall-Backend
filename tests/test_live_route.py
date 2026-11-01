import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from recall import live


def test_live_connection_authenticates_and_starts_session(monkeypatch):
    app = FastAPI()
    app.include_router(live.router, prefix="/api/v1")
    execute = AsyncMock()

    class Engine:
        @asynccontextmanager
        async def begin(self):
            yield SimpleNamespace(execute=execute)

    class Session:
        async def receive(self):
            await asyncio.Event().wait()
            yield None

    @asynccontextmanager
    async def connect(**kwargs):
        yield Session()

    app.state.engine = Engine()
    app.state.genai_live = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect)))
    monkeypatch.setattr(live, "user_from_token", AsyncMock(return_value="student"))
    monkeypatch.setattr(live, "_reserve", AsyncMock(return_value=(30, 123)))
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/live") as socket:
            socket.send_json({"type": "start", "token": "test", "file_id": None, "language": None})
            assert socket.receive_json() == {"type": "ready", "seconds_left": 30, "output_rate": 24000}
            socket.send_json({"type": "stop"})
    assert execute.await_count == 1


def test_live_connection_rejects_invalid_auth(monkeypatch):
    app = FastAPI()
    app.include_router(live.router, prefix="/api/v1")
    app.state.engine = object()
    monkeypatch.setattr(live, "user_from_token", AsyncMock(side_effect=HTTPException(status_code=401, detail="Invalid token")))
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/live") as socket:
            socket.send_json({"type": "start", "token": "invalid"})
            assert socket.receive_json() == {"type": "error", "message": "Invalid token"}
