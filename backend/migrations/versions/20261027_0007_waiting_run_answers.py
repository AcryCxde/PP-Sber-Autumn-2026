"""Persist same-run clarification state.

Revision ID: 20261027_0007
Revises: 20261012_0006
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261027_0007"
down_revision: str | None = "20261012_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("pending_answer_id", sa.String(length=36), nullable=True))
    op.add_column("runs", sa.Column("pending_question_id", sa.String(length=128), nullable=True))
    op.add_column("runs", sa.Column("pending_question", sa.JSON(), nullable=True))
    op.add_column("runs", sa.Column("pending_answers", sa.JSON(), nullable=True))
    op.create_index("uq_runs_pending_answer_id", "runs", ["pending_answer_id"], unique=True)
    op.create_table("run_answer_receipts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("question_id", sa.String(length=128), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("accepted_revision", sa.Integer(), nullable=False),
        sa.Column("accepted_status", sa.String(length=32), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_run_answer_receipts_conversation_id", "run_answer_receipts", ["conversation_id"])
    op.create_index("ix_run_answer_receipts_run_id", "run_answer_receipts", ["run_id"])
    op.drop_index("uq_runs_one_active_per_conversation", table_name="runs")
    op.create_index("uq_runs_one_active_per_conversation", "runs", ["conversation_id"], unique=True,
                    sqlite_where=sa.text("status IN ('pending', 'running', 'waiting_for_input')"),
                    postgresql_where=sa.text("status IN ('pending', 'running', 'waiting_for_input')"))


def downgrade() -> None:
    op.drop_index("ix_run_answer_receipts_run_id", table_name="run_answer_receipts")
    op.drop_index("ix_run_answer_receipts_conversation_id", table_name="run_answer_receipts")
    op.drop_table("run_answer_receipts")
    op.drop_index("uq_runs_one_active_per_conversation", table_name="runs")
    op.create_index("uq_runs_one_active_per_conversation", "runs", ["conversation_id"], unique=True,
                    sqlite_where=sa.text("status IN ('pending', 'running')"),
                    postgresql_where=sa.text("status IN ('pending', 'running')"))
    op.drop_index("uq_runs_pending_answer_id", table_name="runs")
    for column in ("pending_answers", "pending_question", "pending_question_id", "pending_answer_id"):
        op.drop_column("runs", column)
