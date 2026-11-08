from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Configuration via environment or .env (spec §8). No remote LLM endpoints."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    demo_login_enabled: bool = Field(default=True, validation_alias="DEMO_LOGIN_ENABLED")
    gmail_background_enabled: bool = Field(default=True, validation_alias="GMAIL_BACKGROUND_ENABLED")

    ollama_base_url: str = Field(
        default="http://localhost:11434/v1",
        validation_alias="OLLAMA_BASE_URL",
        description="Local Ollama OpenAI-compatible endpoint.",
    )
    ollama_model: str = Field(
        default="", validation_alias="OLLAMA_MODEL",
        description="Required; user-selected model from `ollama list`. Never hard-coded.",
    )
    classification_confidence_threshold: float = Field(
        default=0.75, ge=0.0, le=1.0,
        validation_alias="CLASSIFICATION_CONFIDENCE_THRESHOLD",
    )
    wait_window_minutes: int = Field(default=30, validation_alias="WAIT_WINDOW_MINUTES")
    max_file_size_mb: int = Field(default=10, gt=0, validation_alias="MAX_FILE_SIZE_MB")
    max_text_chars: int = Field(default=40000, gt=0, validation_alias="MAX_TEXT_CHARS")
    llm_timeout_seconds: float = Field(default=120.0, gt=0, validation_alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=2, ge=0, le=5, validation_alias="LLM_MAX_RETRIES")
    workspace_dir: Path = Field(default=Path("./data/workspace"), validation_alias="WORKSPACE_DIR")
    database_url: str = Field(default="sqlite:///./data/freight.db", validation_alias="DATABASE_URL")
    fx_rates_json: str = Field(
        default="{}", validation_alias="FX_RATES_JSON",
        description='Explicit FX rates for comparison only, e.g. '
                    '{"USD->EUR": {"rate": 0.92, "as_of": "2026-10-28"}}. '
                    "Without an entry, foreign-currency quotes are never summed or ranked.",
    )
    google_oauth_credentials_path: str = Field(
        default="", validation_alias="GOOGLE_OAUTH_CREDENTIALS_PATH",
        description="Protected local path to an OAuth 2.0 client-secrets JSON file.",
    )
    google_oauth_token_path: str = Field(
        default="data/google-oauth-token.json", validation_alias="GOOGLE_OAUTH_TOKEN_PATH",
        description="Protected local path for Gmail refresh-token metadata. Never browser-visible.",
    )
    gmail_sync_interval_seconds: int = Field(
        default=30, ge=5, validation_alias="GMAIL_SYNC_INTERVAL_SECONDS",
        description="Automatic Gmail polling interval in seconds.",
    )
    gmail_backfill_days: int = Field(
        default=30, ge=1, validation_alias="GMAIL_BACKFILL_DAYS",
        description="Initial Gmail historical backfill window in days.",
    )

    @property
    def project_credentials_path(self) -> Path:
        if not self.google_oauth_credentials_path:
            return Path("").resolve()
        path = Path(self.google_oauth_credentials_path).expanduser()
        return (path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve())

    @property
    def project_oauth_token_path(self) -> Path:
        path = Path(self.google_oauth_token_path).expanduser()
        return (path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve())

    @property
    def ollama_root_url(self) -> str:
        """Base URL without the /v1 OpenAI-compatible suffix, for /api/tags health checks."""
        url = self.ollama_base_url.rstrip("/")
        return url[: -len("/v1")] if url.endswith("/v1") else url

    @property
    def database_path(self) -> Path:
        """Absolute path to the SQLite database file."""
        if self.database_url.startswith("sqlite:///"):
            raw = self.database_url[len("sqlite:///"):]
            return Path(raw).resolve()
        return Path(self.database_url).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def is_local_endpoint(base_url: str) -> bool:
    """MVP keeps inference local: only localhost/Unix-style local Ollama endpoints."""
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "host.docker.internal", "ollama"}
