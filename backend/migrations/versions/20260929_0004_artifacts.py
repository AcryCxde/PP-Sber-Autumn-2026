"""Move file snapshots out of event payloads.

Revision ID: 20260929_0004
Revises: 20260929_0003
Create Date: 2026-09-29
"""

from collections.abc import Sequence
import json

from alembic import op
import sqlalchemy as sa


revision: str = "20260929_0004"
down_revision: str | None = "20260929_0003"
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
    artifacts = op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "event_seq", name="uq_artifacts_run_event"),
    )
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"], unique=False)

    events = sa.table(
        "events",
        sa.column("id", sa.Integer()),
        sa.column("run_id", sa.String()),
        sa.column("seq", sa.Integer()),
        sa.column("type", sa.String()),
        sa.column("payload", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            events.c.id,
            events.c.run_id,
            events.c.seq,
            events.c.payload,
            events.c.created_at,
        ).where(events.c.type == "tool_result")
    )

    for event_id, run_id, event_seq, raw_payload, created_at in rows:
        payload = as_dict(raw_payload)
        if not isinstance(payload, dict):
            continue
        file_payload = payload.get("file")
        if not isinstance(file_payload, dict):
            continue
        file_path = file_payload.get("path")
        if not isinstance(file_path, str) or not file_path:
            continue

        file_content = file_payload.pop("content", None)
        size_bytes = file_payload.get("content_size_bytes")
        if isinstance(file_content, str):
            actual_size = len(file_content.encode("utf-8"))
            if not isinstance(size_bytes, int):
                size_bytes = actual_size
        truncated = file_payload.get("content_truncated") is True
        if isinstance(size_bytes, int) and size_bytes > MAX_FILE_CONTENT_BYTES:
            truncated = True
        stored_content = (
            file_content
            if isinstance(file_content, str) and not truncated
            else None
        )

        file_payload["content_size_bytes"] = size_bytes
        file_payload["content_truncated"] = truncated
        connection.execute(
            artifacts.insert().values(
                run_id=run_id,
                event_seq=event_seq,
                path=file_path,
                content=stored_content,
                size_bytes=size_bytes,
                truncated=truncated,
                created_at=created_at,
            )
        )
        connection.execute(
            events.update()
            .where(events.c.id == event_id)
            .values(payload=payload)
        )


def downgrade() -> None:
    events = sa.table(
        "events",
        sa.column("id", sa.Integer()),
        sa.column("run_id", sa.String()),
        sa.column("seq", sa.Integer()),
        sa.column("payload", sa.JSON()),
    )
    artifacts = sa.table(
        "artifacts",
        sa.column("run_id", sa.String()),
        sa.column("event_seq", sa.Integer()),
        sa.column("content", sa.Text()),
    )
    connection = op.get_bind()

    rows = connection.execute(
        sa.select(
            artifacts.c.run_id,
            artifacts.c.event_seq,
            artifacts.c.content,
        )
    )
    for run_id, event_seq, content in rows:
        event_row = connection.execute(
            sa.select(events.c.id, events.c.payload).where(
                events.c.run_id == run_id,
                events.c.seq == event_seq,
            )
        ).one_or_none()
        if event_row is None:
            continue
        event_id, raw_payload = event_row
        payload = as_dict(raw_payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("file"), dict):
            continue
        payload["file"]["content"] = content
        payload["file"].pop("content_size_bytes", None)
        payload["file"].pop("content_truncated", None)
        connection.execute(
            events.update().where(events.c.id == event_id).values(payload=payload)
        )

    op.drop_index("ix_artifacts_run_id", table_name="artifacts")
    op.drop_table("artifacts")
