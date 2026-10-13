"""Redact unsafe legacy events and invalidate old executor state.

Revision ID: 20261012_0006
Revises: 20260929_0005
Create Date: 2026-10-12
"""

from collections.abc import Mapping, Sequence
import json

from alembic import op
import sqlalchemy as sa


revision: str = "20261012_0006"
down_revision: str | None = "20260929_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SAFE_FAILURE_CODES = {
    "artifact_save_failed",
    "backend_task_cancelled",
    "claude_cli_failed",
    "claude_request_failed",
    "claude_session_mismatch",
    "claude_session_unavailable",
    "result_error",
}


def as_dict(value) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def safe_message(value) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    result = {}
    for key in (
        "id",
        "conversation_id",
        "run_id",
        "role",
        "kind",
        "sequence",
        "content",
        "created_at",
    ):
        item = value.get(key)
        if isinstance(item, (str, int)) or item is None:
            result[key] = item
    return result


def upgrade() -> None:
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.String()),
        sa.column("claude_session_id", sa.String()),
        sa.column("context_state", sa.String()),
    )
    runs = sa.table(
        "runs",
        sa.column("id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("resume_session_id", sa.String()),
        sa.column("claude_session_id", sa.String()),
        sa.column("error", sa.Text()),
        sa.column("error_code", sa.String()),
    )
    events = sa.table(
        "events",
        sa.column("id", sa.Integer()),
        sa.column("run_id", sa.String()),
        sa.column("type", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    artifacts = sa.table("artifacts", sa.column("id", sa.Integer()))
    connection = op.get_bind()

    connection.execute(artifacts.delete())
    connection.execute(
        conversations.update().values(
            claude_session_id=None,
            context_state="unavailable",
        )
    )
    connection.execute(
        runs.update().values(
            resume_session_id=None,
            claude_session_id=None,
            error=None,
        )
    )

    run_rows = connection.execute(
        sa.select(runs.c.id, runs.c.status, runs.c.error_code)
    )
    run_state = {
        run_id: (status, error_code)
        for run_id, status, error_code in run_rows
    }

    rows = connection.execute(
        sa.select(events.c.id, events.c.run_id, events.c.type, events.c.payload)
    )
    for event_id, run_id, event_type, raw_payload in rows:
        payload = as_dict(raw_payload)
        new_type = event_type

        if event_type == "run.accepted":
            safe_payload = {"schema_version": 1}
            for key in ("client_request_id", "context_mode"):
                value = payload.get(key)
                if isinstance(value, str):
                    safe_payload[key] = value
            message = safe_message(payload.get("message"))
            if message is not None:
                safe_payload["message"] = message
        elif event_type == "message":
            new_type = "message.created"
            safe_payload = {"schema_version": 1}
            role = payload.get("role")
            content = payload.get("content")
            if isinstance(role, str):
                safe_payload["role"] = role
            if isinstance(content, str):
                safe_payload["content"] = content
        elif event_type == "context.reset":
            safe_payload = {"schema_version": 1}
            message = safe_message(payload.get("message"))
            if message is not None:
                safe_payload["message"] = message
        elif event_type == "result":
            new_type = "run.completed"
            safe_payload = {"schema_version": 1, "status": "completed"}
        elif event_type == "run.failed":
            status, error_code = run_state.get(run_id, ("failed", None))
            code = (
                error_code
                if error_code in SAFE_FAILURE_CODES
                else "claude_request_failed"
            )
            safe_payload = {
                "schema_version": 1,
                "status": status if status in {"failed", "cancelled"} else "failed",
                "error_code": code,
                "message": "Запрос Claude завершился с ошибкой.",
                "retryable": code in {"claude_cli_failed", "result_error"},
                "can_reset_context": True,
            }
        else:
            new_type = "legacy.redacted"
            safe_payload = {"schema_version": 1, "redacted": True}

        connection.execute(
            events.update()
            .where(events.c.id == event_id)
            .values(type=new_type, payload=safe_payload)
        )


def downgrade() -> None:
    # Removed secrets, session state, and artifact snapshots cannot be restored safely.
    pass
