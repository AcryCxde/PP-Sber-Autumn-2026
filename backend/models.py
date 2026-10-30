from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class SafeText(TypeDecorator[str]):
    """Text, в который можно записать вывод агента: Postgres не хранит NUL, он заменяется на U+FFFD."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        del dialect
        if value is not None and "\x00" in value:
            return value.replace("\x00", "\ufffd")
        return value


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Хранится нормализованным (strip + lower): уникальность не зависит от регистра.
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # user | admin; права по роли пока не проверяются.
    role: Mapped[str] = mapped_column(String(20), default="user", server_default="user")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    # SHA-256 от токена из cookie: утечка таблицы не даёт готовых сессий.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # NULL у диалогов, созданных до появления пользователей: их не видит никто.
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    claude_session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    context_state: Mapped[str] = mapped_column(String(20), default="new")
    next_message_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        index=True,
    )

    runs: Mapped[list[Run]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.sequence",
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index(
            "uq_runs_one_active_per_conversation",
            "conversation_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    client_request_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        index=True,
    )
    context_mode: Mapped[str] = mapped_column(String(20), default="new")
    resume_session_id: Mapped[str | None] = mapped_column(String(100))
    claude_session_id: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(64))
    next_event_seq: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    turns: Mapped[int | None] = mapped_column(Integer)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(SafeText)

    conversation: Mapped[Conversation] = relationship(back_populates="runs")
    messages: Mapped[list[Message]] = relationship(back_populates="run")
    events: Mapped[list[Event]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="Event.seq",
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    artifact_confirmations: Mapped[list[ArtifactConfirmation]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "sequence",
            name="uq_messages_conversation_sequence",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(32), default="text")
    sequence: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(SafeText)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    run: Mapped[Run | None] = relationship(back_populates="messages")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_events_run_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )

    run: Mapped[Run] = relationship(back_populates="events")


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "event_seq", name="uq_artifacts_run_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        index=True,
    )
    event_seq: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(SafeText)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Заполняются, когда результат зафиксирован в git-репозитории проекта.
    git_path: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    git_tree_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )

    run: Mapped[Run] = relationship(back_populates="artifacts")


class ArtifactConfirmation(Base):
    __tablename__ = "artifact_confirmations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirming', 'confirmed', 'conflicted')",
            name="ck_artifact_confirmations_status",
        ),
        UniqueConstraint(
            "run_id",
            "artifact_id",
            name="uq_artifact_confirmations_run_artifact",
        ),
        Index(
            "ix_artifact_confirmations_status_next_attempt_at",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        index=True,
    )
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    artifact_id: Mapped[str] = mapped_column(String(64))
    source_event_seq: Mapped[int] = mapped_column(Integer)
    version_id: Mapped[str] = mapped_column(String(100))
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_category: Mapped[str | None] = mapped_column(String(64))
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    run: Mapped[Run] = relationship(back_populates="artifact_confirmations")


def empty_answers() -> list[list[str]]:
    return [[], [], []]


def empty_custom() -> list[str]:
    return ["", "", ""]


class Project(Base):
    """Проект CoreTeams: подготовка задачи и три этапа работы в своём диалоге."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        unique=True,
    )
    scenario: Mapped[str] = mapped_column(String(20), default="other")
    example: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(120), default="")
    problem: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[str] = mapped_column(Text, default="")
    answers: Mapped[list[list[str]]] = mapped_column(JSON, default=empty_answers)
    custom: Mapped[list[str]] = mapped_column(JSON, default=empty_custom)
    team_feedback: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    setup_step: Mapped[str] = mapped_column(String(20), default="welcome")
    question_index: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[int] = mapped_column(Integer, default=0)
    # draft | active | ready | done; состояние этапа выводится из его запусков.
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    feedback: Mapped[str] = mapped_column(String(20), default="")
    feedback_note: Mapped[str] = mapped_column(Text, default="")
    git_repo_url: Mapped[str | None] = mapped_column(Text)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        index=True,
    )

    runs: Mapped[list[ProjectRun]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectRun.created_at",
    )
    files: Mapped[list[ProjectFile]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectFile.created_at",
    )


class ProjectRun(Base):
    """Запуск Claude в рамках этапа: начало этапа, доработка или ответ на вопрос агента."""

    __tablename__ = "project_runs"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )
    stage: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )

    project: Mapped[Project] = relationship(back_populates="runs")
    run: Mapped[Run] = relationship()


class ProjectFile(Base):
    __tablename__ = "project_files"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_project_files_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )

    project: Mapped[Project] = relationship(back_populates="files")


class ConversationFile(Base):
    """Документ, приложенный к свободному диалогу; у проектов — свои ProjectFile."""

    __tablename__ = "conversation_files"
    __table_args__ = (
        UniqueConstraint("conversation_id", "name", name="uq_conversation_files_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


class RunnerContainer(Base):
    """Экземпляр runner-контейнера диалога: от создания до удаления по простою или вместе с томами.

    Контейнер одноразовый, поэтому у диалога может быть много записей; открытая — со статусом
    running. Источник правды о живых контейнерах — Docker, таблица хранит их историю.
    """

    __tablename__ = "containers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
    )
    last_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    docker_container_id: Mapped[str | None] = mapped_column(String(255))
    container_name: Mapped[str] = mapped_column(String(255))
    agent_role: Mapped[str] = mapped_column(String(100))
    image_tag: Mapped[str] = mapped_column(String(255))
    network_name: Mapped[str | None] = mapped_column(String(255))
    # running | retired (тома сохранены) | discarded (удалён с томами) | gone (пропал вне backend)
    status: Mapped[str] = mapped_column(String(20), index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
