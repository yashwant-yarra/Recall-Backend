import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import google.auth
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from google.cloud import storage
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .config import settings
from recall.abuse import trial_refusal
from recall.db import make_engine, paid_plan, trial_state
from . import auth, files, search, ask, agent_api, billing, quiz, speech, live, tasks_api
from google import genai

from .embeddings import make_genai_client

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = make_engine()
    app.state.engine = engine
    credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    app.state.gcs_credentials = credentials
    app.state.gcs_bucket = storage.Client(credentials=credentials, project=project).bucket(settings.gcs_bucket)
    app.state.genai = make_genai_client()
    # Gemini Live is only served from one region, so it gets its own client.
    app.state.genai_live = genai.Client(vertexai=True, project=settings.google_cloud_project, location=settings.live_location)
    yield
    await engine.dispose()


app = FastAPI(lifespan=lifespan)
# Lets the browser frontend call the API from its own origin. Auth is a Bearer header, not cookies,
# so credentials stay off; only the methods and headers the API uses are allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=3600,  # browsers cache the preflight for an hour instead of asking before every call
)
app.include_router(files.router, prefix="/api/v1")
app.include_router(search.router, prefix="/api/v1")
app.include_router(ask.router, prefix="/api/v1")
app.include_router(agent_api.router, prefix="/api/v1")
app.include_router(billing.router, prefix="/api/v1")
app.include_router(quiz.router, prefix="/api/v1")
app.include_router(speech.router, prefix="/api/v1")
app.include_router(live.router, prefix="/api/v1")
app.include_router(tasks_api.router, prefix="/api/v1")

logger = logging.getLogger(__name__)


@app.get("/health")
def read_health():
    return {"status": "ok"}


@app.get("/health/db")
async def read_health_db(request: Request):
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except OperationalError as e:
        logger.exception("Database health check failed")
        raise HTTPException(status_code=503, detail="Database connection failed") from e


def plan_limits() -> list[dict]:
    """What each tier includes, for the UI's plan table. A free value of 0 means 'paid tiers only';
    a value of None means unlimited. Read from settings so the screen can't drift from the real limits."""
    s, mb = settings, 1024 * 1024

    def row(key, label, free, pro, ultra):
        return {"key": key, "label": label, "free": free, "pro": pro, "ultra": ultra}

    return [
        row("questions", "Questions per week", s.daily_questions, s.pro_daily_questions, s.ultra_daily_questions),
        row("uploads", "Uploads per week", s.daily_uploads, s.pro_daily_uploads, s.ultra_daily_uploads),
        row("quizzes", "Quizzes per week", s.daily_quizzes, s.pro_daily_quizzes, s.ultra_daily_quizzes),
        row("voice_inputs", "Spoken questions per week", s.daily_voice_inputs, s.pro_daily_voice_inputs, s.ultra_daily_voice_inputs),
        row("read_aloud", "Read-aloud requests per week", s.daily_tts_requests, s.pro_daily_tts_requests, s.ultra_daily_tts_requests),
        row("live_minutes", "Talk minutes per week", s.daily_live_seconds // 60, s.pro_daily_live_seconds // 60, s.ultra_daily_live_seconds // 60),
        row("agent_runs", "Agent runs per week", s.agent_daily_runs, s.pro_agent_daily_runs, s.ultra_agent_daily_runs),
        row("web_searches", "Web search answers per week", s.daily_web_searches, s.pro_daily_web_searches, s.ultra_daily_web_searches),
        row("max_files", "Files kept at once", s.free_max_files, s.pro_max_files, None),
        row("max_storage_mb", "Total storage (MB)", s.free_max_storage // mb, s.pro_max_storage // mb, s.ultra_max_storage // mb),
        row("max_file_mb", "Largest file (MB)", s.free_max_upload_size // mb, s.pro_max_upload_size // mb, s.max_upload_size // mb),
    ]


@app.get("/me")
async def read_current_user(request: Request, who: dict = Depends(auth.identity_from_request)):
    user_id = who["user_id"]
    async with request.app.state.engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT plan, trial_ends_at, paid_until FROM users WHERE id = :uid"), {"uid": user_id})).one()
    now = datetime.now(timezone.utc)
    # The trial uses the stored plan, like POST /billing/trial: anyone who ever paid has no trial to start.
    trial = trial_state(row.plan, row.trial_ends_at, now)
    # blocked: why a trial that is still available cannot start yet (unverified or throwaway email), in words.
    trial["blocked"] = trial_refusal(who["email"], who["email_verified"]) if trial["available"] else None
    # plan is the pass the user has right now (free once it runs out); paid_until is when it ends.
    plan = paid_plan(row.plan, row.paid_until, now)
    return {"user_id": user_id, "plan": plan, "paid_until": row.paid_until.isoformat() if plan != "free" else None,
            "email_verified": who["email_verified"], "trial": trial, "limits": plan_limits()}
