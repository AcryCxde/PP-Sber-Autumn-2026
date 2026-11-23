"""Add immutable image digests to application deployment evidence."""

from alembic import op
import sqlalchemy as sa

revision = "20261123_0002"
down_revision = "20261123_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployment_audits", sa.Column("backend_digest", sa.String(71), nullable=True)
    )
    op.add_column(
        "deployment_audits", sa.Column("frontend_digest", sa.String(71), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("deployment_audits", "frontend_digest")
    op.drop_column("deployment_audits", "backend_digest")
