import logging
from contextlib import asynccontextmanager

import google.auth
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from google.cloud import storage
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .config import settings
from recall.db import make_engine
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


@app.get("/me")
async def read_current_user(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    async with request.app.state.engine.connect() as conn:
        plan = (await conn.execute(text("SELECT plan FROM users WHERE id = :uid"), {"uid": user_id})).scalar_one()
    return {"user_id": user_id, "plan": plan}
