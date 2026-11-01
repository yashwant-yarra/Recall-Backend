import sqlite3
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from recall import auth
from recall.agent_api import router


@pytest.fixture
def agent_api():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE agent_runs (id TEXT PRIMARY KEY, user_id TEXT, status TEXT, goal TEXT, answer TEXT, error TEXT);
        CREATE TABLE agent_steps (run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE);
    """)
    ids = [str(uuid4()) for _ in range(4)]
    db.executemany("INSERT INTO agent_runs VALUES (?, ?, ?, 'goal', 'answer', 'error')", [
        (ids[0], "me", "done"), (ids[1], "other", "done"),
        (ids[2], "me", "running"), (ids[3], "me", "awaiting_approval"),
    ])
    db.executemany("INSERT INTO agent_steps VALUES (?)", [(id,) for id in ids])
    db.commit()

    class Connection:
        async def execute(self, statement, params):
            row = db.execute(str(statement), {k: str(v) for k, v in params.items()}).fetchone()

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
        yield client, db, ids
    db.close()


def test_delete_owned_run_and_steps(agent_api):
    client, db, ids = agent_api
    assert client.delete(f"/api/v1/agent/runs/{ids[0]}").status_code == 204
    assert db.execute("SELECT run_id FROM agent_steps ORDER BY run_id").fetchall() == sorted((id,) for id in ids[1:])
    assert db.execute("SELECT goal, answer, error FROM agent_runs WHERE id = ?", (ids[0],)).fetchone() == ("", None, None)
    assert client.delete(f"/api/v1/agent/runs/{ids[0]}").status_code == 404


def test_delete_rejects_other_users_and_active_runs(agent_api):
    client, db, ids = agent_api
    assert client.delete(f"/api/v1/agent/runs/{ids[1]}").status_code == 404
    assert client.delete(f"/api/v1/agent/runs/{uuid4()}").status_code == 404
    for id in ids[2:]:
        assert client.delete(f"/api/v1/agent/runs/{id}").status_code == 409
    assert client.delete("/api/v1/agent/runs/invalid").status_code == 422
    assert db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 4


def test_delete_requires_authentication(agent_api, monkeypatch):
    client, _, ids = agent_api
    monkeypatch.setattr(auth.settings, "dev_user_id", None)
    client.app.dependency_overrides.clear()
    assert client.delete(f"/api/v1/agent/runs/{ids[0]}").status_code == 401
