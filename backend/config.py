from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
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
    # S3-совместимое хранилище файлов агента (SeaweedFS в docker-стеке); без endpoint выключено.
    s3_endpoint_url: str | None = None
    s3_bucket: str = "coreteams-artifacts"
    s3_access_key: str | None = Field(default=None, repr=False)
    s3_secret_key: str | None = Field(default=None, repr=False)
    s3_region: str = "us-east-1"
    # Файл больше этого не сохраняется в хранилище (в базе остаётся только запись о нём).
    artifact_max_storage_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    # Сколько файлов, изменённых агентом не через Write, подбирать после запуска в режиме cli.
    artifact_scan_max_files: int = Field(default=50, ge=0)
    artifact_scan_max_entries: int = Field(default=10000, ge=1)
    subscription_window_seconds: float = Field(default=15.0, gt=0)
    subscription_poll_interval_seconds: float = Field(default=0.25, gt=0)
    max_concurrent_runs: int = Field(default=4, gt=0)

    auth_session_ttl_seconds: int = Field(default=14 * 86400, gt=0)
    # true за HTTPS; по http браузер не вернёт Secure-cookie и вход не сработает.
    auth_cookie_secure: bool = False
    # Legacy compatibility only; unverified email addresses never grant administrator roles.
    admin_emails: str = ""
    # Включать только после enforcement всех путей запуска и race-проверок.
    admin_policy_management_enabled: bool = False
    # Explicit browser origin at the trusted edge; never infer it from forwarded headers.
    admin_public_origin: str | None = None
    runner_harness_diagnostics_enabled: bool = False

    @field_validator("admin_public_origin")
    @classmethod
    def validate_admin_public_origin(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        message = "admin_public_origin must be a canonical HTTP(S) origin (lowercase ASCII host, no default port)"
        if any(ord(character) <= 32 or ord(character) >= 127 for character in value):
            raise ValueError(message)
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError(message)
        if ":" in host:
            if "%" in host:
                raise ValueError(message)
            canonical_host = f"[{ip_address(host).compressed}]"
        else:
            if any(not (character.isalnum() or character in ".-") for character in host):
                raise ValueError(message)
            # Reject browser-specific short/numeric IPv4 spellings instead of guessing them.
            last_label = host.rsplit(".", 1)[-1]
            if last_label.isdigit() or last_label.startswith("0x"):
                canonical_host = str(ip_address(host))
            else:
                canonical_host = host
        port = parsed.port
        suffix = "" if port is None or port == {"http": 80, "https": 443}[parsed.scheme] else f":{port}"
        if value != f"{parsed.scheme}://{canonical_host}{suffix}":
            raise ValueError(message)
        return value

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
