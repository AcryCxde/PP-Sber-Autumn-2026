"""Store agent files in object storage: key, hash and media type of the original bytes.

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
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("storage_key", sa.Text(), nullable=True))
        batch.add_column(sa.Column("sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("content_type", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_column("content_type")
        batch.drop_column("sha256")
        batch.drop_column("storage_key")
