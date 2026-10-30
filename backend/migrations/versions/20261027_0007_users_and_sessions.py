"""Add users, auth sessions and conversation ownership.

Revision ID: 20261027_0007
Revises: 20261012_0006
Create Date: 2026-10-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261027_0007"
down_revision: str | None = "20261012_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])

    # Существующие диалоги остаются без владельца и скрыты от всех пользователей:
    # привязать их к кому-то автоматически нельзя.
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("owner_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_conversations_owner_id_users",
            "users",
            ["owner_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_conversations_owner_id", ["owner_id"])


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_index("ix_conversations_owner_id")
        batch.drop_constraint("fk_conversations_owner_id_users", type_="foreignkey")
        batch.drop_column("owner_id")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
