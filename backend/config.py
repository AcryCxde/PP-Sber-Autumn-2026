from pathlib import Path
from typing import Literal

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
    artifact_max_download_bytes: int = 10 * 1024 * 1024

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
