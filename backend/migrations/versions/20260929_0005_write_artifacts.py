"""Backfill Write tool artifacts and redact tool input content.

Revision ID: 20260929_0005
Revises: 20260929_0004
Create Date: 2026-09-29
"""

from collections.abc import Sequence
import json

from alembic import op
import sqlalchemy as sa


revision: str = "20260929_0005"
down_revision: str | None = "20260929_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_FILE_CONTENT_BYTES = 10 * 1024 * 1024


def as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return None


def upgrade() -> None:
    events = sa.table(
        "events",
        sa.column("id", sa.Integer()),
        sa.column("run_id", sa.String()),
        sa.column("seq", sa.Integer()),
        sa.column("type", sa.String()),
        sa.column("payload", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    artifacts = sa.table(
        "artifacts",
        sa.column("run_id", sa.String()),
        sa.column("event_seq", sa.Integer()),
        sa.column("path", sa.Text()),
        sa.column("content", sa.Text()),
        sa.column("size_bytes", sa.Integer()),
        sa.column("truncated", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    write_calls: dict[tuple[str, str], dict] = {}
    current_run_id: str | None = None

    rows = connection.execute(
        sa.select(
            events.c.id,
            events.c.run_id,
            events.c.seq,
            events.c.type,
            events.c.payload,
            events.c.created_at,
        ).order_by(events.c.run_id, events.c.seq)
    )
    for event_id, run_id, event_seq, event_type, raw_payload, created_at in rows:
        if run_id != current_run_id:
            write_calls.clear()
            current_run_id = run_id

        payload = as_dict(raw_payload)
        if not isinstance(payload, dict):
            continue

        if event_type == "tool" and payload.get("name") == "Write":
            tool_id = payload.get("tool_id")
            tool_input = payload.get("input")
            if not isinstance(tool_id, str) or not isinstance(tool_input, dict):
                continue
            file_path = tool_input.get("file_path")
            file_content = tool_input.pop("content", None)
            if not isinstance(file_path, str) or not isinstance(file_content, str):
                continue
            size_bytes = len(file_content.encode("utf-8"))
            truncated = size_bytes > MAX_FILE_CONTENT_BYTES
            write_calls[(run_id, tool_id)] = {
                "path": file_path,
                "content": None if truncated else file_content,
                "size_bytes": size_bytes,
                "truncated": truncated,
            }
            tool_input["content_size_bytes"] = size_bytes
            tool_input["content_redacted"] = True
            connection.execute(
                events.update().where(events.c.id == event_id).values(payload=payload)
            )
            continue

        if event_type != "tool_result":
            continue
        tool_id = payload.get("tool_id")
        write_call = write_calls.pop((run_id, tool_id), None) if isinstance(tool_id, str) else None
        if write_call is None or isinstance(payload.get("file"), dict):
            continue

        existing_artifact = connection.execute(
            sa.select(artifacts.c.run_id).where(
                artifacts.c.run_id == run_id,
                artifacts.c.event_seq == event_seq,
            )
        ).one_or_none()
        if existing_artifact is None:
            connection.execute(
                artifacts.insert().values(
                    run_id=run_id,
                    event_seq=event_seq,
                    path=write_call["path"],
                    content=write_call["content"],
                    size_bytes=write_call["size_bytes"],
                    truncated=write_call["truncated"],
                    created_at=created_at,
                )
            )

        payload["file"] = {
            "path": write_call["path"],
            "content_size_bytes": write_call["size_bytes"],
            "content_truncated": write_call["truncated"],
            "num_lines": (
                write_call["content"].count("\n") + 1
                if isinstance(write_call["content"], str)
                else None
            ),
        }
        connection.execute(
            events.update().where(events.c.id == event_id).values(payload=payload)
        )


def downgrade() -> None:
    # Migration 0004 restores artifact content to tool_result events before
    # dropping the artifact table. Write input content stays redacted because
    # duplicating persisted file bodies in public tool events is unsafe.
    pass
