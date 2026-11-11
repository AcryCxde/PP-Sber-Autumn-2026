"""Persist immutable answer request fingerprints.

Revision ID: 20261111_0004
Revises: 20261111_0003
"""

from alembic import op
import sqlalchemy as sa

revision = "20261111_0004"
down_revision = "20261111_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_runs", sa.Column("request_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("project_runs", "request_hash")
