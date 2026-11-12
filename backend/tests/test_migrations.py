import json
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
        "artifacts",
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
                3,
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
        connection.execute(
            "INSERT INTO events (run_id, seq, type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "run-1",
                1,
                "tool_result",
                json.dumps(
                    {
                        "schema_version": 1,
                        "file": {
                            "path": "legacy.txt",
                            "content": "legacy snapshot",
                            "num_lines": 1,
                        },
                    }
                ),
                "2026-09-29 00:00:00",
            ),
        )
        connection.execute(
            "INSERT INTO events (run_id, seq, type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "run-1",
                2,
                "tool",
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "Write",
                        "tool_id": "write-tool",
                        "input": {
                            "file_path": "written.txt",
                            "content": "written snapshot",
                        },
                    }
                ),
                "2026-09-29 00:00:00",
            ),
        )
        connection.execute(
            "INSERT INTO events (run_id, seq, type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "run-1",
                3,
                "tool_result",
                json.dumps(
                    {
                        "schema_version": 1,
                        "tool_id": "write-tool",
                        "content": "File created successfully",
                    }
                ),
                "2026-09-29 00:00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "20260929_0005")

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
        artifact = connection.execute(
            "SELECT path, content, size_bytes, truncated FROM artifacts "
            "WHERE run_id = 'run-1' AND event_seq = 1"
        ).fetchone()
        event_payload = json.loads(
            connection.execute(
                "SELECT payload FROM events WHERE run_id = 'run-1' AND seq = 1"
            ).fetchone()[0]
        )
        write_artifact = connection.execute(
            "SELECT path, content, size_bytes, truncated FROM artifacts "
            "WHERE run_id = 'run-1' AND event_seq = 3"
        ).fetchone()
        write_tool_payload = json.loads(
            connection.execute(
                "SELECT payload FROM events WHERE run_id = 'run-1' AND seq = 2"
            ).fetchone()[0]
        )
        write_result_payload = json.loads(
            connection.execute(
                "SELECT payload FROM events WHERE run_id = 'run-1' AND seq = 3"
            ).fetchone()[0]
        )

    assert conversation == ("active", 1)
    assert run is not None
    assert len(run[0]) == 36
    assert run[1] == "session-1"
    assert kind == ("text",)
    assert artifact == ("legacy.txt", "legacy snapshot", 15, 0)
    assert "content" not in event_payload["file"]
    assert event_payload["file"]["content_size_bytes"] == 15
    assert event_payload["file"]["content_truncated"] is False
    assert write_artifact == ("written.txt", "written snapshot", 16, 0)
    assert "content" not in write_tool_payload["input"]
    assert write_tool_payload["input"]["content_redacted"] is True
    assert write_result_payload["file"]["path"] == "written.txt"
    assert write_result_payload["file"]["content_size_bytes"] == 16


def test_security_migration_redacts_legacy_history(tmp_path) -> None:
    database_path = tmp_path / "sensitive-legacy.db"
    config = migration_config(database_path)
    command.upgrade(config, "20260929_0005")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO conversations "
            "(id, title, status, claude_session_id, context_state, next_message_seq, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "conversation-1",
                "Sensitive",
                "active",
                "secret-session",
                "active",
                1,
                "2026-10-12 00:00:00",
                "2026-10-12 00:00:00",
            ),
        )
        for run_id, status, request_id in (
            ("run-completed", "completed", "00000000-0000-0000-0000-000000000001"),
            ("run-failed", "failed", "00000000-0000-0000-0000-000000000002"),
        ):
            connection.execute(
                "INSERT INTO runs "
                "(id, conversation_id, status, client_request_id, context_mode, "
                "resume_session_id, claude_session_id, error_code, next_event_seq, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    "conversation-1",
                    status,
                    request_id,
                    "resume",
                    "secret-resume",
                    "secret-run-session",
                    "claude_cli_failed",
                    8 if status == "completed" else 1,
                    "secret stderr",
                ),
            )

        legacy_events = [
            (1, "run.accepted", {"client_request_id": "request", "secret": "accepted-secret"}),
            (2, "session", {"session_id": "session-secret", "cwd": "/host/private"}),
            (3, "tool", {"input": {"content": "tool-secret"}}),
            (4, "tool_result", {"content": "result-secret"}),
            (5, "thinking", {"content": "thinking-secret"}),
            (6, "raw", {"content": "stderr-secret"}),
            (7, "unknown.future", {"secret": "unknown-secret"}),
            (8, "result", {"session_id": "terminal-secret", "cost_usd": 10}),
        ]
        for seq, event_type, payload in legacy_events:
            connection.execute(
                "INSERT INTO events (run_id, seq, type, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "run-completed",
                    seq,
                    event_type,
                    json.dumps(payload),
                    "2026-10-12 00:00:00",
                ),
            )
        connection.execute(
            "INSERT INTO events (run_id, seq, type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "run-failed",
                1,
                "run.failed",
                json.dumps({"message": "failure-secret"}),
                "2026-10-12 00:00:00",
            ),
        )
        connection.execute(
            "INSERT INTO artifacts "
            "(run_id, event_seq, path, content, size_bytes, truncated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "run-completed",
                4,
                "/host/private/.env",
                "artifact-secret",
                15,
                0,
                "2026-10-12 00:00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "20261012_0006")

    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        conversation = connection.execute(
            "SELECT claude_session_id, context_state FROM conversations"
        ).fetchone()
        runs = connection.execute(
            "SELECT resume_session_id, claude_session_id, error FROM runs ORDER BY id"
        ).fetchall()
        artifacts = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()
        events = connection.execute(
            "SELECT seq, type, payload FROM events WHERE run_id = 'run-completed' "
            "ORDER BY seq"
        ).fetchall()
        failed = connection.execute(
            "SELECT type, payload FROM events WHERE run_id = 'run-failed'"
        ).fetchone()

    assert version == ("20261012_0006",)
    assert conversation == (None, "unavailable")
    assert runs == [(None, None, None), (None, None, None)]
    assert artifacts == (0,)
    assert [row[0] for row in events] == list(range(1, 9))
    assert events[-1][1] == "run.completed"
    for _, event_type, raw_payload in events[1:7]:
        assert event_type == "legacy.redacted"
        assert json.loads(raw_payload) == {"schema_version": 1, "redacted": True}
    serialized = json.dumps(events) + json.dumps(failed)
    for secret in (
        "accepted-secret",
        "session-secret",
        "tool-secret",
        "result-secret",
        "thinking-secret",
        "stderr-secret",
        "unknown-secret",
        "terminal-secret",
        "failure-secret",
        "artifact-secret",
        "/host/private",
    ):
        assert secret not in serialized


def test_admin_ai_access_additive_upgrade_preserves_history(tmp_path) -> None:
    database_path = tmp_path / "admin-policy.db"
    config = migration_config(database_path)
    command.upgrade(config, "20261111_0005")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users (id,email,password_hash,created_at) "
            "VALUES ('owner','owner@example.test','hash','2026-11-11')"
        )
        connection.execute(
            "INSERT INTO conversations "
            "(id,owner_id,title,status,context_state,next_message_seq,created_at,updated_at) "
            "VALUES ('conversation','owner','History','active','new',0,'2026-11-11','2026-11-11')"
        )
        connection.execute(
            "INSERT INTO runs "
            "(id,conversation_id,status,client_request_id,context_mode,next_event_seq) "
            "VALUES ('historical','conversation','completed','request','new',0)"
        )
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT ai_frozen,ai_policy_version FROM users WHERE id='owner'"
        ).fetchone() == (0, 0)
        assert connection.execute(
            "SELECT status,started_at,cost_usd,admitted_at,admission_policy_version,"
            "harness_estimate FROM runs WHERE id='historical'"
        ).fetchone() == ("completed", None, None, None, None, None)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_policy_audits'"
        ).fetchone() == ("ai_policy_audits",)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO ai_policy_audits "
            "(id,actor_id,target_id,action,reason,before_version,after_version,"
            "idempotency_key,request_hash,response) "
            "VALUES ('audit','owner','owner','freeze','private',0,1,'key','hash','{}')"
        )
        connection.commit()
        import pytest

        for action, reason, before, after, key in (
            ("freeze", "private", 0, 1, "key"),
            ("freeze", None, 0, 1, "missing-reason"),
            ("freeze", "   ", 0, 1, "empty-reason"),
            ("freeze", "x" * 1001, 0, 1, "long-reason"),
            ("unfreeze", None, -1, 0, "negative-version"),
            ("unfreeze", None, 1, 3, "jump-version"),
            ("invalid", "note", 0, 1, "invalid-action"),
            ("unfreeze", None, 1, 2, ""),
            ("unfreeze", None, 1, 2, "x" * 65),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO ai_policy_audits "
                    "(id,actor_id,target_id,action,reason,before_version,after_version,"
                    "idempotency_key,request_hash,response) "
                    "VALUES ('invalid','owner','owner',?,?,?,?,?,'hash','{}')",
                    (action, reason, before, after, key),
                )
            connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE users SET ai_policy_version=-1 WHERE id='owner'")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id='owner'")
        connection.rollback()

    command.downgrade(config, "20261111_0005")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT status FROM runs").fetchone() == ("completed",)
        assert "ai_frozen" not in {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
    command.upgrade(config, "head")


def test_admin_policy_constraints_on_selected_database(tmp_path) -> None:
    import asyncio
    from datetime import UTC, datetime
    import pytest
    from sqlalchemy import insert, select
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import create_async_engine
    from backend.models import AiPolicyAudit, User
    from backend.tests.databases import database_url

    url = database_url(tmp_path / "policy-constraints.db")
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")

    async def check():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.execute(insert(User).values(id="owner", email="owner@test.local",
                    password_hash="hash", created_at=datetime.now(UTC)))
            async with engine.connect() as connection:
                row = (await connection.execute(select(User.ai_frozen, User.ai_policy_version))).one()
                assert tuple(row) == (False, 0)
            valid = dict(id="first", actor_id="owner", target_id="owner", action="freeze",
                reason="private", before_version=0, after_version=1, idempotency_key="same",
                request_hash="hash", response={})
            async with engine.begin() as connection:
                await connection.execute(insert(AiPolicyAudit).values(**valid))
            for overrides in ({"id": "dup"}, {"id": "blank", "idempotency_key": "blank", "reason": "   "},
                              {"id": "null", "idempotency_key": "null", "reason": None},
                              {"id": "badversion", "idempotency_key": "badversion", "after_version": 4}):
                with pytest.raises(IntegrityError):
                    async with engine.begin() as connection:
                        await connection.execute(insert(AiPolicyAudit).values(**{**valid, **overrides}))
        finally:
            await engine.dispose()
    asyncio.run(check())
    command.downgrade(config, "20261111_0005")
    command.upgrade(config, "head")


def test_migrations_match_models_and_0009_downgrades(tmp_path) -> None:
    """На SQLite и (с TEST_POSTGRES_URL) на Postgres схема после миграций совпадает с моделями."""
    import asyncio

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend import models  # noqa: F401
    from backend.db import Base
    from backend.tests.databases import database_url

    url = database_url(tmp_path / "drift.db")
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = url

    def inspect_schema(connection):
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
        inspector = inspect(connection)
        return diff, set(inspector.get_table_names()), {
            column["name"] for column in inspector.get_columns("users")
        }

    async def snapshot():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(inspect_schema)
        finally:
            await engine.dispose()

    command.upgrade(config, "head")
    diff, tables, user_columns = asyncio.run(snapshot())
    assert diff == []
    assert "containers" in tables and "role" in user_columns

    command.downgrade(config, "20261027_0008")
    _, tables, user_columns = asyncio.run(snapshot())
    assert "containers" not in tables and "role" not in user_columns

    command.upgrade(config, "head")
