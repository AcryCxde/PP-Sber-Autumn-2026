import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


BACKEND_DIR = Path(__file__).resolve().parents[1]


def migration_config(database_path: Path) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = (
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    return config


def test_initial_migration_creates_history_tables_and_parent_directory(
    tmp_path,
) -> None:
    database_path = tmp_path / "missing" / "directory" / "migration.db"
    command.upgrade(migration_config(database_path), "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        run_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }
        conversation_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(conversations)")
        }
        message_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)")
        }

    assert {
        "next_event_seq",
        "client_request_id",
        "context_mode",
        "resume_session_id",
        "claude_session_id",
        "error_code",
    } <= run_columns
    assert {"context_state", "next_message_seq"} <= conversation_columns
    assert "kind" in message_columns
    assert {
        "alembic_version",
        "conversations",
        "runs",
        "messages",
        "events",
    } <= tables


def test_multiturn_migration_backfills_legacy_history(tmp_path) -> None:
    database_path = tmp_path / "legacy.db"
    config = migration_config(database_path)
    command.upgrade(config, "20260929_0002")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO conversations "
            "(id, title, status, claude_session_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "conversation-1",
                "Legacy",
                "active",
                "session-1",
                "2026-09-29 00:00:00",
                "2026-09-29 00:00:00",
            ),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, conversation_id, status, next_event_seq, started_at, completed_at, "
            "duration_ms, cost_usd, turns, exit_code, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                "conversation-1",
                "completed",
                0,
                None,
                "2026-09-29 00:00:01",
                None,
                None,
                None,
                0,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO messages "
            "(id, conversation_id, run_id, role, sequence, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "message-1",
                "conversation-1",
                "run-1",
                "user",
                1,
                "Legacy prompt",
                "2026-09-29 00:00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        conversation = connection.execute(
            "SELECT context_state, next_message_seq FROM conversations "
            "WHERE id = 'conversation-1'"
        ).fetchone()
        run = connection.execute(
            "SELECT client_request_id, claude_session_id FROM runs WHERE id = 'run-1'"
        ).fetchone()
        kind = connection.execute(
            "SELECT kind FROM messages WHERE id = 'message-1'"
        ).fetchone()

    assert conversation == ("active", 1)
    assert run is not None
    assert len(run[0]) == 36
    assert run[1] == "session-1"
    assert kind == ("text",)
