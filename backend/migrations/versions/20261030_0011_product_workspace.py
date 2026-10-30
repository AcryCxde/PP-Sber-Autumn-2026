"""Add the product workspace (contract V2): client IDs, file hashes, product runs and approvals.

Revision ID: 20261030_0011
Revises: 20261029_0010
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261030_0011"
down_revision: str | None = "20261029_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("client_project_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("product", sa.JSON(), nullable=True))
        batch.create_unique_constraint(
            "uq_projects_owner_client_project", ["owner_id", "client_project_id"]
        )
    with op.batch_alter_table("project_files") as batch:
        batch.add_column(sa.Column("client_file_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("sha256", sa.String(length=64), nullable=True))
        batch.create_unique_constraint(
            "uq_project_files_client_file", ["project_id", "client_file_id"]
        )

    op.create_table(
        "product_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_request_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("stage_id", sa.String(length=64), nullable=False),
        sa.Column("parent_run_id", sa.String(length=36), nullable=True),
        sa.Column("source_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_product_runs_project_id", "product_runs", ["project_id"])

    with op.batch_alter_table("project_runs") as batch:
        batch.add_column(sa.Column("product_run_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("display_text", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_project_runs_product_run",
            "product_runs",
            ["product_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_project_runs_product_run_id", ["product_run_id"])

    op.create_table(
        "artifact_approvals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_run_id",
            sa.String(length=36),
            sa.ForeignKey("product_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("client_request_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "artifact_id", name="uq_artifact_approvals_artifact"),
    )


def downgrade() -> None:
    op.drop_table("artifact_approvals")
    with op.batch_alter_table("project_runs") as batch:
        batch.drop_index("ix_project_runs_product_run_id")
        batch.drop_constraint("fk_project_runs_product_run", type_="foreignkey")
        batch.drop_column("display_text")
        batch.drop_column("product_run_id")
    op.drop_index("ix_product_runs_project_id", table_name="product_runs")
    op.drop_table("product_runs")
    with op.batch_alter_table("project_files") as batch:
        batch.drop_constraint("uq_project_files_client_file", type_="unique")
        batch.drop_column("sha256")
        batch.drop_column("client_file_id")
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("uq_projects_owner_client_project", type_="unique")
        batch.drop_column("product")
        batch.drop_column("client_project_id")
