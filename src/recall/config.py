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
    trial_days: int = 7  # one free trial per account, opt-in, no card; it gives Pro (the middle tier)
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
    quiz_model: str = "gemini-3.5-flash"  # writing good questions and grading answers needs the larger model
    ocr_model: str = "gemini-3.5-flash"  # reads scanned PDF pages when they have no text layer
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
    # Razorpay billing. Unset keys = billing switched off (the billing endpoints return 503).
    razorpay_key_id: str | None = None  # rzp_test_... (identifies the account; not secret)
    razorpay_key_secret: str | None = None  # Secret Manager in production
    razorpay_webhook_secret: str | None = None  # chosen when creating the webhook; Secret Manager
    # Two monthly Razorpay plans, one per paid tier. The original plan (the one existing subscribers pay for)
    # is the Pro plan; Ultra is sold through a newer, higher-priced plan. Unset = that tier cannot be bought
    # yet (503). Both are plain env vars, not secrets.
    razorpay_plan_id: str | None = None  # RAZORPAY_PLAN_ID: plan_... of the monthly Ultra plan
    razorpay_pro_plan_id: str | None = None  # RAZORPAY_PRO_PLAN_ID: plan_... of the monthly Pro plan
    razorpay_total_count: int = 12  # billing cycles per subscription (Razorpay requires a number)
    razorpay_api_url: str = "https://api.razorpay.com/v1"
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