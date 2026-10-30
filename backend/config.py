from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
DEFAULT_DATABASE_PATH = BACKEND_DIR / "data" / "history.db"


class Settings(BaseSettings):
    database_url: str = f"sqlite+aiosqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"
    claude_permission_mode: Literal[
        "acceptEdits",
        "auto",
        "dontAsk",
        "manual",
        "plan",
    ] = "acceptEdits"
    artifact_max_download_bytes: int = Field(
        default=10 * 1024 * 1024,
        gt=0,
    )
    artifact_confirmation_batch_size: int = Field(default=20, gt=0, le=100)
    artifact_confirmation_max_attempts: int = Field(default=8, gt=0, le=100)
    artifact_confirmation_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    artifact_confirmation_lease_seconds: float = Field(default=60.0, gt=0, le=3600)
    artifact_confirmation_max_retry_delay_seconds: float = Field(default=300.0, gt=0, le=86400)
    artifact_confirmation_reconcile_interval_seconds: float = Field(default=5.0, gt=0, le=3600)
    workspace_root: Path = Path.home() / ".claude-web" / "workspaces"
    subscription_window_seconds: float = Field(default=15.0, gt=0)
    subscription_poll_interval_seconds: float = Field(default=0.25, gt=0)
    max_concurrent_runs: int = Field(default=4, gt=0)

    auth_session_ttl_seconds: int = Field(default=14 * 86400, gt=0)
    # true за HTTPS; по http браузер не вернёт Secure-cookie и вход не сработает.
    auth_cookie_secure: bool = False

    # "cli" — claude подпроцессом в контейнере backend; "runner" — контейнер ctrunner на сессию.
    executor: Literal["cli", "runner"] = "cli"
    anthropic_auth_token: str | None = Field(default=None, repr=False)
    runner_image: str = "coreteams-runner:dev"
    runner_network: str | None = None
    runner_gateway_url: str = "ws://backend:8000/v1/runner"
    runner_transport_db: Path = BACKEND_DIR / "data" / "runner_transport.db"
    runner_secret_file: Path = BACKEND_DIR / "data" / "runner_gateway.secret"
    runner_context_dir: Path | None = None
    runner_memory: str = "1g"
    runner_cpus: float = Field(default=1.0, gt=0)
    runner_stall_after_seconds: float = Field(default=720.0, gt=0)
    runner_heartbeat_timeout_seconds: float = Field(default=30.0, gt=0)
    runner_connect_timeout_seconds: float = Field(default=180.0, gt=0)
    runner_idle_timeout_seconds: float = Field(default=600.0, gt=0)
    # Тома сессии без контейнера хранятся столько секунд с последней активности; 0 — всегда.
    runner_volume_retention_seconds: float = Field(default=30 * 86400.0, ge=0)
    runner_supervise_interval_seconds: float = Field(default=5.0, gt=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
