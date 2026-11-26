"""Store requested output format and immutable exported artifact metadata.

Revision ID: 20261126_0001
Revises: 20261124_0001
"""

from alembic import op
import sqlalchemy as sa


revision = "20261126_0001"
down_revision = "20261124_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product_runs",
        sa.Column("output_format", sa.String(length=10), nullable=False, server_default="md"),
    )
    op.create_table(
        "artifact_exports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_artifact_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("requested_format", sa.String(length=10), nullable=False),
        sa.Column("detected_format", sa.String(length=10), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "requested_format IN ('docx', 'pdf', 'xlsx', 'pptx')",
            name="ck_artifact_exports_requested_format",
        ),
        sa.CheckConstraint("status IN ('pending', 'ready', 'failed')", name="ck_artifact_exports_status"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["artifacts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_artifact_id",
            "requested_format",
            name="uq_artifact_exports_source_format",
        ),
    )
    op.create_index("ix_artifact_exports_run_id", "artifact_exports", ["run_id"])
    op.create_index(
        "ix_artifact_exports_source_artifact_id",
        "artifact_exports",
        ["source_artifact_id"],
    )
    with op.batch_alter_table("artifact_approvals") as batch:
        batch.add_column(sa.Column("export_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("output_format", sa.String(length=10), nullable=True))
        batch.add_column(sa.Column("export_sha256", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_artifact_approvals_export_id",
            "artifact_exports",
            ["export_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("artifact_approvals") as batch:
        batch.drop_constraint("fk_artifact_approvals_export_id", type_="foreignkey")
        batch.drop_column("export_sha256")
        batch.drop_column("output_format")
        batch.drop_column("export_id")
    op.drop_index("ix_artifact_exports_source_artifact_id", table_name="artifact_exports")
    op.drop_index("ix_artifact_exports_run_id", table_name="artifact_exports")
    op.drop_table("artifact_exports")
    op.drop_column("product_runs", "output_format")
