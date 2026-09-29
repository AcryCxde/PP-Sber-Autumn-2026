import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_initial_migration_creates_history_tables_and_parent_directory(
    tmp_path,
) -> None:
    database_path = tmp_path / "missing" / "directory" / "migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = database_url

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {"alembic_version", "conversations", "runs", "messages"} <= tables
