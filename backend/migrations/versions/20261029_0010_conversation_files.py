"""Add documents attached to standalone conversations.

Revision ID: 20261029_0010
Revises: 20261027_0009
Create Date: 2026-10-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261029_0010"
down_revision: str | None = "20261027_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_files",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "name", name="uq_conversation_files_name"),
    )
    op.create_index(
        "ix_conversation_files_conversation_id",
        "conversation_files",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_files_conversation_id", table_name="conversation_files")
    op.drop_table("conversation_files")
