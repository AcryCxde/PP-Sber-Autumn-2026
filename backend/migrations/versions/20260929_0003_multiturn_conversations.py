"""Add multi-turn conversation state.

Revision ID: 20260929_0003
Revises: 20260929_0002
Create Date: 2026-09-29
"""

from collections.abc import Sequence
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "20260929_0003"
down_revision: str | None = "20260929_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "context_state",
            sa.String(length=20),
            server_default="new",
            nullable=False,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "next_message_seq",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "runs",
        sa.Column("client_request_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column(
            "context_mode",
            sa.String(length=20),
            server_default="new",
            nullable=False,
        ),
    )
    op.add_column(
        "runs",
        sa.Column("resume_session_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("claude_session_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column(
            "kind",
            sa.String(length=32),
            server_default="text",
            nullable=False,
        ),
    )

    connection = op.get_bind()
    conversation_rows = connection.execute(
        sa.text("SELECT id, claude_session_id FROM conversations")
    ).fetchall()
    for conversation_id, session_id in conversation_rows:
        max_sequence = connection.execute(
            sa.text(
                "SELECT COALESCE(MAX(sequence), 0) "
                "FROM messages WHERE conversation_id = :conversation_id"
            ),
            {"conversation_id": conversation_id},
        ).scalar_one()
        run_count = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM runs WHERE conversation_id = :conversation_id"
            ),
            {"conversation_id": conversation_id},
        ).scalar_one()
        context_state = "active" if session_id else ("unavailable" if run_count else "new")
        connection.execute(
            sa.text(
                "UPDATE conversations "
                "SET next_message_seq = :next_message_seq, context_state = :context_state "
                "WHERE id = :conversation_id"
            ),
            {
                "next_message_seq": max_sequence,
                "context_state": context_state,
                "conversation_id": conversation_id,
            },
        )

    run_rows = connection.execute(
        sa.text("SELECT id, conversation_id FROM runs")
    ).fetchall()
    for run_id, conversation_id in run_rows:
        session_id = connection.execute(
            sa.text(
                "SELECT claude_session_id FROM conversations WHERE id = :conversation_id"
            ),
            {"conversation_id": conversation_id},
        ).scalar_one_or_none()
        connection.execute(
            sa.text(
                "UPDATE runs SET client_request_id = :client_request_id, "
                "claude_session_id = :claude_session_id WHERE id = :run_id"
            ),
            {
                "client_request_id": str(uuid4()),
                "claude_session_id": session_id,
                "run_id": run_id,
            },
        )

    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column(
            "client_request_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )

    op.create_index(
        "ix_runs_client_request_id",
        "runs",
        ["client_request_id"],
        unique=True,
    )
    op.create_index(
        "uq_runs_one_active_per_conversation",
        "runs",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'running')"),
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_runs_one_active_per_conversation", table_name="runs")
    op.drop_index("ix_runs_client_request_id", table_name="runs")

    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("kind")

    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("error_code")
        batch_op.drop_column("claude_session_id")
        batch_op.drop_column("resume_session_id")
        batch_op.drop_column("context_mode")
        batch_op.drop_column("client_request_id")

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("next_message_seq")
        batch_op.drop_column("context_state")
