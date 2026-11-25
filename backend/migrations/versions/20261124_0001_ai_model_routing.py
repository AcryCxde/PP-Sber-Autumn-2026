"""Persist Claude Code model routing, checks and changes."""

from alembic import op
import sqlalchemy as sa

revision = "20261124_0001"
down_revision = "20261123_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_model_routing",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("primary_model", sa.String(128)),
        sa.Column("reserve_model", sa.String(128)),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_model", sa.String(128)),
        sa.Column("reason", sa.String(32)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id = 1", name="ck_ai_model_routing_singleton"),
    )
    op.create_table(
        "ai_model_health",
        sa.Column("model", sa.String(128), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(32)),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("compatible", sa.Boolean(), nullable=False),
        sa.Column("reported_model", sa.String(128)),
    )
    op.create_table(
        "ai_model_routing_audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(254)),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("before", sa.JSON()),
        sa.Column("after", sa.JSON()),
    )
    op.create_index("ix_ai_model_routing_audits_created_id", "ai_model_routing_audits", ["created_at", "id"])
    op.add_column("runs", sa.Column("execution_model", sa.String(128)))


def downgrade() -> None:
    op.drop_column("runs", "execution_model")
    op.drop_index("ix_ai_model_routing_audits_created_id", table_name="ai_model_routing_audits")
    op.drop_table("ai_model_routing_audits")
    op.drop_table("ai_model_health")
    op.drop_table("ai_model_routing")
