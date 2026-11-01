import sqlite3
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from recall import auth
from recall.ask import router


@pytest.fixture
def chat_api():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE chats (id TEXT PRIMARY KEY, user_id TEXT);
        CREATE TABLE messages (chat_id TEXT REFERENCES chats(id) ON DELETE CASCADE);
    """)
    owned, other = str(uuid4()), str(uuid4())
    db.executemany("INSERT INTO chats VALUES (?, ?)", [(owned, "me"), (other, "someone-else")])
    db.executemany("INSERT INTO messages VALUES (?)", [(owned,), (other,)])
    db.commit()

    class Connection:
        async def execute(self, statement, params):
            cursor = db.execute(str(statement), {k: str(v) for k, v in params.items()})
            row = cursor.fetchone()

            class Result:
                def scalar_one_or_none(self):
                    return row[0] if row else None

            return Result()

    class Engine:
        @asynccontextmanager
        async def begin(self):
            with db:
                yield Connection()

    app = FastAPI()
    app.state.engine = Engine()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[auth.get_current_user_id] = lambda: "me"
    with TestClient(app) as client:
        yield client, db, owned, other
    db.close()


def test_delete_chat_removes_messages_and_keeps_other_chats(chat_api):
    client, db, owned, other = chat_api
    response = client.delete(f"/api/v1/chats/{owned}")
    assert response.status_code == 204
    assert response.content == b""
    assert db.execute("SELECT id FROM chats").fetchall() == [(other,)]
    assert db.execute("SELECT chat_id FROM messages").fetchall() == [(other,)]
    assert client.delete(f"/api/v1/chats/{owned}").status_code == 404


def test_delete_chat_rejects_other_users_and_missing_chats(chat_api):
    client, db, owned, other = chat_api
    for chat_id in (other, str(uuid4())):
        response = client.delete(f"/api/v1/chats/{chat_id}")
        assert response.status_code == 404
        assert response.json() == {"detail": "Chat not found"}
    assert db.execute("SELECT count(*) FROM chats").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
    assert client.delete("/api/v1/chats/invalid").status_code == 422


def test_delete_chat_requires_authentication(chat_api, monkeypatch):
    client, _, owned, _ = chat_api
    monkeypatch.setattr(auth.settings, "dev_user_id", None)
    client.app.dependency_overrides.clear()
    assert client.delete(f"/api/v1/chats/{owned}").status_code == 401
