"""Add durable artifact confirmation intents.

Revision ID: 20261030_0011
Revises: 20261029_0010
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261030_0011"
down_revision: str | None = "20261029_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_confirmations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("transport_run_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("source_event_seq", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.String(length=100), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'confirming', 'confirmed', 'conflicted')",
            name="ck_artifact_confirmations_status",
        ),
        sa.UniqueConstraint(
            "run_id",
            "artifact_id",
            name="uq_artifact_confirmations_run_artifact",
        ),
    )
    op.create_index(
        "ix_artifact_confirmations_run_id",
        "artifact_confirmations",
        ["run_id"],
    )
    op.create_index(
        "ix_artifact_confirmations_project_id",
        "artifact_confirmations",
        ["project_id"],
    )
    op.create_index(
        "ix_artifact_confirmations_status_next_attempt_at",
        "artifact_confirmations",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_artifact_confirmations_status_next_attempt_at",
        table_name="artifact_confirmations",
    )
    op.drop_index("ix_artifact_confirmations_project_id", table_name="artifact_confirmations")
    op.drop_index("ix_artifact_confirmations_run_id", table_name="artifact_confirmations")
    op.drop_table("artifact_confirmations")
