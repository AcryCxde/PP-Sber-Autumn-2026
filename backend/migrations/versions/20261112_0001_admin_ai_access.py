"""Add private AI policy audits and nullable Run admission metadata.

Revision ID: 20261112_0001
Revises: 20261111_0005
"""

from alembic import op
import sqlalchemy as sa

revision = "20261112_0001"
down_revision = "20261111_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("ai_frozen", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("ai_policy_version", sa.Integer(), nullable=False, server_default="0"))
        batch.create_check_constraint("ck_users_ai_policy_version", "ai_policy_version >= 0")
    op.add_column("runs", sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("admission_policy_version", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("harness_estimate", sa.JSON(), nullable=True))
    op.create_index("ix_runs_started_id", "runs", ["started_at", "id"])
    op.create_table(
        "ai_policy_audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("target_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_version", sa.Integer(), nullable=False),
        sa.Column("after_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.UniqueConstraint("actor_id", "idempotency_key", name="uq_ai_policy_audits_actor_key"),
        sa.CheckConstraint("action IN ('freeze', 'unfreeze')", name="ck_ai_policy_audits_action"),
        sa.CheckConstraint("before_version >= 0 AND after_version = before_version + 1", name="ck_ai_policy_audits_versions"),
        sa.CheckConstraint("length(idempotency_key) BETWEEN 1 AND 64", name="ck_ai_policy_audits_key"),
        # Structural guard; service validates Unicode-normalized nonblank reasons.
        sa.CheckConstraint(
            "(reason IS NULL AND action = 'unfreeze') OR "
            "(reason IS NOT NULL AND length(reason) <= 1000 AND "
            "(action = 'unfreeze' OR length(trim(reason)) > 0))",
            name="ck_ai_policy_audits_reason",
        ),
    )
    op.create_index("ix_ai_policy_audits_target_created_id", "ai_policy_audits", ["target_id", "created_at", "id"])


def downgrade() -> None:
    # Только isolated fixture; operational downgrade при действующих freezes запрещён.
    op.drop_index("ix_ai_policy_audits_target_created_id", table_name="ai_policy_audits")
    op.drop_table("ai_policy_audits")
    op.drop_index("ix_runs_started_id", table_name="runs")
    op.drop_column("runs", "harness_estimate")
    op.drop_column("runs", "admission_policy_version")
    op.drop_column("runs", "admitted_at")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("ck_users_ai_policy_version", type_="check")
        batch.drop_column("ai_policy_version")
        batch.drop_column("ai_frozen")
