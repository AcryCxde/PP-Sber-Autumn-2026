"""Persist selected project file IDs per product run.

Revision ID: 20261111_0005
Revises: 20261111_0004
"""

from alembic import op
import sqlalchemy as sa

revision = "20261111_0005"
down_revision = "20261111_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_runs", sa.Column("file_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("product_runs", "file_ids")
