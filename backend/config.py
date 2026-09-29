from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE_PATH = BACKEND_DIR / "data" / "history.db"


class Settings(BaseSettings):
    database_url: str = f"sqlite+aiosqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
