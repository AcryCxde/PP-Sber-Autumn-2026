"""Persist application-owned deployment evidence."""

from alembic import op
import sqlalchemy as sa

revision = "20261123_0001"
down_revision = "20261112_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployment_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="deployed"),
        sa.Column("release_sha", sa.String(40), nullable=True),
        sa.Column("workflow_run_id", sa.BigInteger(), nullable=True),
        sa.Column("run_attempt", sa.Integer(), nullable=True),
        sa.Column("migration_head", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "release_sha",
            "workflow_run_id",
            "run_attempt",
            name="uq_deployment_audits_release",
        ),
    )
    op.execute(
        "INSERT INTO deployment_audits(kind,migration_head) VALUES ('schema_initialized','20261123_0001')"
    )


def downgrade() -> None:
    op.drop_table("deployment_audits")
