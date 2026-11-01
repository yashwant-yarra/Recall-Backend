import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from recall import auth, files
from recall.config import settings
from recall.db import require_pro
from recall.main import plan_limits


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    scalar_one_or_none = scalar_one


class FakeEngine:
    """Answers the plan lookup and the file count; anything else (an insert) means a cap did not stop the request."""

    def __init__(self, plan, kept=0):
        self.plan, self.kept = plan, kept

    def connect(self):
        engine = self

        class Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, statement, params=None):
                sql = str(statement)
                if "SELECT plan" in sql:
                    return FakeResult(engine.plan)
                if "count(*)" in sql:
                    return FakeResult(engine.kept)
                raise AssertionError(f"unexpected query: {sql}")

        return Conn()

    begin = connect


def test_free_has_no_costly_features_and_pro_keeps_them():
    rows = {r["key"]: r for r in plan_limits()}
    for key in ("live_minutes", "agent_runs", "web_searches"):
        assert rows[key]["free"] == 0 and rows[key]["pro"] > 0  # Pro only
    assert rows["max_files"]["free"] == settings.free_max_files and rows["max_files"]["pro"] is None
    assert rows["max_file_mb"]["free"] < rows["max_file_mb"]["pro"]
    assert len(rows) == len(plan_limits())  # every key is unique


def test_require_pro_refuses_free_and_allows_pro():
    with pytest.raises(HTTPException) as e:
        asyncio.run(require_pro(FakeEngine("free"), "u", "The Agent"))
    assert e.value.status_code == 403 and e.value.detail == "The Agent is part of Pro"
    asyncio.run(require_pro(FakeEngine("pro"), "u", "The Agent"))  # no error


def upload_client(engine):
    app = FastAPI()
    app.include_router(files.router)
    app.state.engine = engine
    app.dependency_overrides[auth.get_current_user_id] = lambda: "u"
    return TestClient(app)


def post_file(client, size):
    return client.post("/files", json={"mime_type": "application/pdf", "size_bytes": size})


def test_free_upload_over_5mb_is_refused():
    r = post_file(upload_client(FakeEngine("free")), settings.free_max_upload_size + 1)
    assert r.status_code == 400 and "Upgrade to Pro" in r.json()["detail"]


def test_free_upload_at_the_file_cap_is_refused():
    r = post_file(upload_client(FakeEngine("free", kept=settings.free_max_files)), 1000)
    assert r.status_code == 403 and "Delete one or upgrade" in r.json()["detail"]
