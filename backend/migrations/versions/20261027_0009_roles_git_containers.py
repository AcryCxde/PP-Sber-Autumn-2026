"""Add user roles, git fields of projects and artifacts, and runner container records.

Схема сверена с agent-platforma-infa (init.sql): users.role, projects.git_repo_url,
artifacts.git_path/commit_sha/git_tree_url и таблица containers.

Revision ID: 20261027_0009
Revises: 20261027_0008
Create Date: 2026-10-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261027_0009"
down_revision: str | None = "20261027_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("role", sa.String(length=20), nullable=False, server_default="user")
        )
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("git_repo_url", sa.Text(), nullable=True))
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("git_path", sa.Text(), nullable=True))
        batch.add_column(sa.Column("commit_sha", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("git_tree_url", sa.Text(), nullable=True))

    op.create_table(
        "containers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "last_run_id",
            sa.String(length=36),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("docker_container_id", sa.String(length=255), nullable=True),
        sa.Column("container_name", sa.String(length=255), nullable=False),
        sa.Column("agent_role", sa.String(length=100), nullable=False),
        sa.Column("image_tag", sa.String(length=255), nullable=False),
        sa.Column("network_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_containers_conversation_id", "containers", ["conversation_id"])
    op.create_index("ix_containers_last_run_id", "containers", ["last_run_id"])
    op.create_index("ix_containers_status", "containers", ["status"])


def downgrade() -> None:
    op.drop_index("ix_containers_status", table_name="containers")
    op.drop_index("ix_containers_last_run_id", table_name="containers")
    op.drop_index("ix_containers_conversation_id", table_name="containers")
    op.drop_table("containers")
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_column("git_tree_url")
        batch.drop_column("commit_sha")
        batch.drop_column("git_path")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("git_repo_url")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("role")
