"""Track user activity for the admin engagement panel: last request time and active days.

Revision ID: 20261030_0013
Revises: 20261030_0012
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261030_0013"
down_revision: str | None = "20261030_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "user_activity_days",
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day", sa.Date(), primary_key=True),
    )
    op.create_index("ix_user_activity_days_day", "user_activity_days", ["day"])


def downgrade() -> None:
    op.drop_index("ix_user_activity_days_day", table_name="user_activity_days")
    op.drop_table("user_activity_days")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_seen_at")
