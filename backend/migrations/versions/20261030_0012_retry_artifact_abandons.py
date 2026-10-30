"""Track delivery of terminal artifact confirmation notices.

Revision ID: 20261030_0012
Revises: 20261030_0011
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261030_0012"
down_revision: str | None = "20261030_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("artifact_confirmations") as batch:
        batch.add_column(
            sa.Column("abandon_delivered", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("artifact_confirmations") as batch:
        batch.drop_column("abandon_delivered")
