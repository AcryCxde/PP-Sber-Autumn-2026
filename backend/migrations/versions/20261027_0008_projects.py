"""Add CoreTeams projects, their runs and uploaded materials.

Revision ID: 20261027_0008
Revises: 20261027_0007
Create Date: 2026-10-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261027_0008"
down_revision: str | None = "20261027_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("scenario", sa.String(length=20), nullable=False),
        sa.Column("example", sa.Boolean(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("problem", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("custom", sa.JSON(), nullable=False),
        sa.Column("team_feedback", sa.Text(), nullable=False),
        sa.Column("notes", sa.JSON(), nullable=False),
        sa.Column("setup_step", sa.String(length=20), nullable=False),
        sa.Column("question_index", sa.Integer(), nullable=False),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("feedback", sa.String(length=20), nullable=False),
        sa.Column("feedback_note", sa.Text(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])
    op.create_index("ix_projects_status", "projects", ["status"])
    op.create_index("ix_projects_updated_at", "projects", ["updated_at"])

    op.create_table(
        "project_runs",
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_runs_project_id", "project_runs", ["project_id"])

    op.create_table(
        "project_files",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "name", name="uq_project_files_name"),
    )
    op.create_index("ix_project_files_project_id", "project_files", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_files_project_id", table_name="project_files")
    op.drop_table("project_files")
    op.drop_index("ix_project_runs_project_id", table_name="project_runs")
    op.drop_table("project_runs")
    op.drop_index("ix_projects_updated_at", table_name="projects")
    op.drop_index("ix_projects_status", table_name="projects")
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_table("projects")
