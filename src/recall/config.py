from pydantic_settings import BaseSettings,SettingsConfigDict 

class Settings(BaseSettings):
    database_url: str
    gcs_bucket: str
    model_config = SettingsConfigDict(env_file=".env")
    db_echo: bool = False
    dev_user_id: str | None = None
    max_upload_size: int = 25 * 1024 * 1024  # 25 MB
    upload_url_ttl_seconds: int = 15 * 60  # 15 minutes
    google_cloud_project: str
    vertex_location: str = "global"
    embed_dims: int = 768
    embed_model: str = "gemini-embedding-001"
    answer_model: str = "gemini-3.5-flash-lite"
    transcribe_model: str = "gemini-3.5-transcribe"
    summary_model: str = "gemini-3.5-flash-lite"
    agent_model: str = "gemini-3.5-flash"  # multi-step tool use benefits from the larger model
    agent_max_steps: int = 8  # tool calls per run before it is stopped
    agent_daily_runs: int = 20  # runs per user per UTC day
    # A processing claim older than this is treated as abandoned (the worker crashed) and can be
    # reclaimed. Must exceed the worker's Cloud Run request timeout (default 300 s), so a live
    # request is always killed before its claim expires.
    claim_lease_seconds: int = 15 * 60
    # Claims allowed per file; the next one marks it failed, so a file that keeps crashing the
    # worker can't be retried forever.
    max_claim_attempts: int = 3


settings = Settings()