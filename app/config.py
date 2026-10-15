"""All settings come from environment variables (or a local .env file).

Nothing else in the app should read os.environ directly: it asks get_settings().
Every field here is also listed, with a comment, in .env.example.
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Read env vars; also read .env if it exists. Unknown env vars are ignored.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- General ---
    APP_NAME: str = "broll-engine"
    APP_VERSION: str = "0.2.0"
    LOG_LEVEL: str = "INFO"
    # Base URL used in links the worker sends (callbacks). The API uses the
    # request's own URL for links unless this is set explicitly.
    PUBLIC_BASE_URL: str = ""

    # --- Auth + rate limit ---
    # Comma-separated list of allowed bearer tokens. Empty = auth switched OFF.
    API_TOKENS: str = ""
    RATE_LIMIT_PER_MINUTE: int = 120

    # --- Database ---
    DATABASE_URL: str = "sqlite:///./data/broll.db"

    # --- Storage ---
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    LOCAL_STORAGE_DIR: str = "./data/storage"
    S3_ENDPOINT_URL: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET: str = "broll"
    S3_REGION: str = "us-east-1"

    # --- Signed download links ---
    FILE_LINK_SECRET: str = "change-me-file-link-secret"
    FILE_LINK_TTL_SECONDS: int = 3600

    # --- Uploads ---
    MAX_UPLOAD_MB: int = 100
    ALLOWED_AUDIO_EXTENSIONS: str = "mp3,wav,m4a"
    WORK_DIR: str = "./data/work"

    # --- Callbacks ---
    CALLBACK_SECRET: str = "change-me-callback-secret"
    CALLBACK_MAX_RETRIES: int = 3
    CALLBACK_BACKOFF_SECONDS: float = 1.0
    CALLBACK_TIMEOUT_SECONDS: float = 10.0

    # --- Worker ---
    WORKER_CONCURRENCY: int = 2
    WORKER_POLL_SECONDS: float = 1.0
    # Phase 2 only: how long each fake pipeline stage sleeps.
    FAKE_STAGE_SECONDS: float = 0.5

    # Small helpers so other code does not split strings itself.
    @property
    def api_tokens(self) -> list[str]:
        return [t.strip() for t in self.API_TOKENS.split(",") if t.strip()]

    @property
    def allowed_audio_extensions(self) -> set[str]:
        return {e.strip().lower().lstrip(".") for e in self.ALLOWED_AUDIO_EXTENSIONS.split(",") if e.strip()}


@lru_cache
def get_settings() -> Settings:
    """Build the settings once and reuse them. Tests call get_settings.cache_clear()."""
    return Settings()
