from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    # GCS bucket naming rules: 3-63 chars, lowercase letters, digits, '-', '_', '.'. A bad value
    # (e.g. from a mangled deploy command) now stops startup, so Cloud Run keeps the old revision.
    gcs_bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
    model_config = SettingsConfigDict(env_file=".env")
    db_echo: bool = False
    dev_user_id: str | None = None
    max_upload_size: int = 25 * 1024 * 1024  # 25 MB (Ultra, and the absolute ceiling)
    free_max_upload_size: int = 5 * 1024 * 1024  # Free: 5 MB per file
    pro_max_upload_size: int = 15 * 1024 * 1024  # Pro: 15 MB per file
    # Accounts allowed in without a verified email, comma-separated: the test account given to Razorpay's
    # reviewers. Everyone else must verify their email to use the API (set VERIFICATION_EXEMPT_EMAILS to change).
    verification_exempt_emails: str = "test@genai.dev"
    trial_days: int = 7  # one free trial per account, opt-in, no card; it gives Pro (the middle tier)
    # Days after a pass or trial ends before recall.prune deletes the oldest files over the Free limits.
    storage_grace_days: int = 90
    # Files kept at once (new uploads only; nothing already stored is deleted). Ultra has no cap.
    free_max_files: int = 5
    pro_max_files: int = 50
    # Total stored per user, all files together (new uploads only; nothing already stored is deleted).
    free_max_storage: int = 50 * 1024 * 1024
    pro_max_storage: int = 1024 * 1024 * 1024
    ultra_max_storage: int = 5 * 1024 * 1024 * 1024
    upload_url_ttl_seconds: int = 15 * 60  # 15 minutes
    download_url_ttl_seconds: int = 5 * 60  # preview links: short-lived, re-requested on each open
    google_cloud_project: str
    vertex_location: str = "global"
    embed_dims: int = 768
    embed_model: str = "gemini-embedding-001"
    answer_model: str = "gemini-3.5-flash-lite"
    transcribe_model: str = "gemini-3.5-transcribe"
    summary_model: str = "gemini-3.5-flash-lite"
    agent_model: str = "gemini-3.5-flash"  # multi-step tool use benefits from the larger model
    agent_max_steps: int = 8  # tool calls per run before it is stopped
    # Three tiers: Free, Pro (middle) and Ultra (the former Pro numbers). Names without a prefix are Free,
    # pro_* is Pro and ultra_* is Ultra. Agent, web search and live Talk are for paid tiers: their free
    # value is 0, and the routes also refuse Free users with a clear message (db.require_pro).
    agent_daily_runs: int = 0  # runs per user per UTC week
    daily_uploads: int = 6  # POST /files per user per UTC week
    daily_questions: int = 30  # POST /ask per user per UTC week (each one is a Gemini call)
    pro_daily_uploads: int = 30
    pro_daily_questions: int = 200
    pro_agent_daily_runs: int = 10
    ultra_daily_uploads: int = 80
    ultra_daily_questions: int = 600
    ultra_agent_daily_runs: int = 40
    daily_web_searches: int = 0  # answers that used Google Search grounding per user per UTC week (billed extra)
    pro_daily_web_searches: int = 20
    ultra_daily_web_searches: int = 80
    daily_quizzes: int = 4  # generated quizzes per user per UTC week (grading is part of the quiz)
    pro_daily_quizzes: int = 10
    ultra_daily_quizzes: int = 30
    quiz_model: str = "gemini-3.5-flash-lite"  # shortcut: quiz quality not compared with Flash yet, upgrade if grading is wrong
    ocr_model: str = "gemini-3.5-flash-lite"  # reads scanned PDF pages with no text layer; not compared with Flash yet
    # Voice: questions spoken into the mic, and answers read aloud (counted per request).
    daily_voice_inputs: int = 20
    pro_daily_voice_inputs: int = 100
    ultra_daily_voice_inputs: int = 300
    daily_tts_requests: int = 100  # one request per piece of an answer (~a sentence or two)
    pro_daily_tts_requests: int = 400
    ultra_daily_tts_requests: int = 1200
    tts_model: str = "gemini-2.5-flash-tts"
    tts_voice: str = "Kore"
    max_voice_input_bytes: int = 2 * 1024 * 1024  # ~2 minutes of compressed speech
    max_tts_chars: int = 600
    # Live voice conversation (Gemini Live). The model is only served from us-central1.
    live_model: str = "gemini-live-2.5-flash-native-audio"
    live_location: str = "us-central1"
    daily_live_seconds: int = 0  # free: none (paid tiers only)
    pro_daily_live_seconds: int = 20 * 60
    ultra_daily_live_seconds: int = 60 * 60
    max_live_session_seconds: int = 10 * 60  # keep below the Cloud Run request timeout
    live_web_search: bool = True  # give the voice tutor Google Search (prompt keeps it on the documents' subject)
    # Cashfree billing: Pro and Ultra are prepaid passes bought through payment links. Unset keys = billing
    # switched off (the billing endpoints return 503).
    cashfree_client_id: str | None = None  # identifies the account; not secret
    cashfree_client_secret: str | None = None  # Secret Manager in production; also signs the webhooks
    # Where Cashfree sends the paid event: https://<api>/api/v1/billing/webhook. Required to sell.
    cashfree_notify_url: str | None = None
    # Where Cashfree sends the user back if the checkout popup cannot open (some in-app browsers). Optional.
    cashfree_return_url: str | None = None
    cashfree_api_url: str = "https://api.cashfree.com/pg"  # sandbox: https://sandbox.cashfree.com/pg
    cashfree_api_version: str = "2025-01-01"
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    pro_price_inr: int = 699  # keep in step with PRICES in the UI's src/lib/legal.ts
    ultra_price_inr: int = 1999
    pass_days: int = 30  # one purchase adds this many days
    # Browser origins allowed to call the API (exact scheme + host + port, no trailing slash).
    # Env var as a plain comma-separated list, no JSON quotes to get mangled by a shell:
    # CORS_ORIGINS=http://localhost:3000,https://recall.vercel.app
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]
        return value
    # A processing claim older than this is treated as abandoned (the worker crashed) and can be
    # reclaimed. Must exceed the worker's Cloud Run request timeout (default 300 s), so a live
    # request is always killed before its claim expires.
    claim_lease_seconds: int = 15 * 60
    # Claims allowed per file; the next one marks it failed, so a file that keeps crashing the
    # worker can't be retried forever.
    max_claim_attempts: int = 3


settings = Settings()
