import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from recall import auth, files
from recall.config import settings
from recall.db import require_pro, tier_params, trial_state
from recall.main import plan_limits


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    scalar_one_or_none = scalar_one


class FakeEngine:
    """Answers the tier lookup ('free', 'pro' or 'ultra') and the file count; anything else (an insert)
    means a cap did not stop the request."""

    def __init__(self, tier, kept=0):
        self.tier, self.kept, self.counted = tier, kept, False

    def connect(self):
        engine = self

        class Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, statement, params=None):
                sql = str(statement)
                if "FROM users" in sql:
                    return FakeResult(engine.tier)
                if "count(*)" in sql and "INSERT" not in sql:
                    engine.counted = True  # the stored-file count, not the daily-limit count inside the insert
                    return FakeResult(engine.kept)
                raise AssertionError(f"unexpected query: {sql}")

        return Conn()

    begin = connect


def test_paid_features_are_off_for_free_and_on_for_pro_and_ultra():
    rows = {r["key"]: r for r in plan_limits()}
    for key in ("live_minutes", "agent_runs", "web_searches"):
        assert rows[key]["free"] == 0 and 0 < rows[key]["pro"] < rows[key]["ultra"]


def test_every_limit_steps_up_from_free_to_pro_to_ultra():
    for r in plan_limits():
        ultra = r["ultra"] if r["ultra"] is not None else float("inf")  # None = unlimited
        assert r["free"] <= r["pro"] <= ultra, r["key"]
    assert len({r["key"] for r in plan_limits()}) == len(plan_limits())


def test_ultra_keeps_the_limits_pro_used_to_have():
    rows = {r["key"]: r for r in plan_limits()}
    # Existing subscribers become Ultra, so these must stay what they paid for.
    assert (rows["questions"]["ultra"], rows["uploads"]["ultra"], rows["agent_runs"]["ultra"]) == (500, 50, 50)
    assert (rows["live_minutes"]["ultra"], rows["web_searches"]["ultra"], rows["max_file_mb"]["ultra"]) == (60, 100, 25)


def test_tier_params_binds_all_three():
    assert tier_params(1, 2, 3) == {"free": 1, "pro": 2, "ultra": 3}


def test_require_pro_refuses_free_and_allows_pro_and_ultra():
    with pytest.raises(HTTPException) as e:
        asyncio.run(require_pro(FakeEngine("free"), "u", "The Agent"))
    assert e.value.status_code == 403 and e.value.detail == "The Agent is part of Pro"
    for tier in ("pro", "ultra"):
        asyncio.run(require_pro(FakeEngine(tier), "u", "The Agent"))  # no error


def upload_client(engine):
    app = FastAPI()
    app.include_router(files.router)
    app.state.engine = engine
    app.dependency_overrides[auth.get_current_user_id] = lambda: "u"
    return TestClient(app, raise_server_exceptions=False)


def post_file(client, size):
    return client.post("/files", json={"mime_type": "application/pdf", "size_bytes": size})


def test_free_upload_over_its_size_is_refused():
    r = post_file(upload_client(FakeEngine("free")), settings.free_max_upload_size + 1)
    assert r.status_code == 400 and "Upgrade to Pro" in r.json()["detail"]


def test_pro_upload_over_its_size_is_refused_and_points_to_ultra():
    r = post_file(upload_client(FakeEngine("pro")), settings.pro_max_upload_size + 1)
    assert r.status_code == 400 and "Upgrade to Ultra" in r.json()["detail"]


def test_file_count_caps_by_tier():
    free = post_file(upload_client(FakeEngine("free", kept=settings.free_max_files)), 1000)
    assert free.status_code == 403 and "Pro keeps more" in free.json()["detail"]
    pro = post_file(upload_client(FakeEngine("pro", kept=settings.pro_max_files)), 1000)
    assert pro.status_code == 403 and "Ultra keeps more" in pro.json()["detail"]


def test_ultra_has_no_file_count_check():
    engine = FakeEngine("ultra", kept=10_000)
    post_file(upload_client(engine), 1000)  # goes on to storage, which this test does not set up (a 500)
    assert not engine.counted  # Ultra has no file cap, so the stored files were never counted
    capped = FakeEngine("pro", kept=0)
    post_file(upload_client(capped), 1000)
    assert capped.counted  # while a capped tier does count them


def test_trial_state():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert trial_state("free", None, now) == {"active": False, "ends_at": None, "available": True}
    running = trial_state("free", now + timedelta(days=3), now)
    assert running["active"] and not running["available"]  # one trial per account, even while it runs
    over = trial_state("free", now - timedelta(seconds=1), now)
    assert not over["active"] and not over["available"]  # used up; can't start another
    for paid in ("pro", "ultra"):
        assert not trial_state(paid, None, now)["available"]  # paying users don't need one


def spend_engine(result):
    """An engine whose one statement records `result` as the new usage_events id (None = at the limit)."""
    seen = []

    class Engine:
        def begin(self):
            class Conn:
                async def __aenter__(self_):
                    return self_

                async def __aexit__(self_, *exc):
                    return False

                async def execute(self_, statement, params):
                    seen.append((str(statement), params))
                    return FakeResult(result)

            return Conn()

    return Engine(), seen


def test_spend_counts_in_the_usage_ledger_not_in_the_user_tables():
    from recall.db import spend

    engine, seen = spend_engine(7)
    assert asyncio.run(spend(engine, "u", "question", 15, 150, 500)) is True
    sql, params = seen[0]
    # The count reads usage_events (which "Clear all data" never touches), not messages/files/quizzes/agent_runs.
    assert "FROM usage_events" in sql and "INSERT INTO usage_events" in sql
    for table in ("FROM messages", "FROM files", "FROM quizzes", "FROM agent_runs"):
        assert table not in sql
    assert params["kind"] == "question" and (params["free"], params["pro"], params["ultra"]) == (15, 150, 500)


def test_spend_refuses_at_the_limit():
    from recall.db import spend

    engine, _ = spend_engine(None)  # the conditional insert matched nothing
    assert asyncio.run(spend(engine, "u", "upload", 3, 15, 50)) is False


def test_disposable_domains_and_subdomains_are_caught():
    from recall.abuse import is_disposable

    assert is_disposable("x@mailinator.com") and is_disposable("X@Mailinator.COM")
    assert is_disposable("x@inbox.mailinator.com")  # a subdomain of a listed service
    assert not is_disposable("x@notmailinator.com")  # a different domain that merely ends the same way
    assert not is_disposable("student@gmail.com") and not is_disposable(None) and not is_disposable("no-at-sign")


def test_trial_refusal_needs_a_verified_permanent_email():
    from recall.abuse import trial_refusal

    assert trial_refusal("a@gmail.com", True) is None
    assert "Verify your email" in trial_refusal("a@gmail.com", False)
    assert "permanent email" in trial_refusal("a@yopmail.com", True)
    assert "Verify your email" in trial_refusal("a@yopmail.com", False)  # verification is checked first


def test_trial_endpoint_refuses_before_touching_the_database():
    from recall import billing

    def client(email, verified):
        app = FastAPI()
        app.include_router(billing.router)
        app.dependency_overrides[auth.identity_from_request] = lambda: {"user_id": "u", "email": email, "email_verified": verified}
        return TestClient(app, raise_server_exceptions=False)

    unverified = client("a@gmail.com", False).post("/billing/trial")
    assert unverified.status_code == 403 and "Verify your email" in unverified.json()["detail"]
    throwaway = client("a@mailinator.com", True).post("/billing/trial")
    assert throwaway.status_code == 403 and "permanent email" in throwaway.json()["detail"]
