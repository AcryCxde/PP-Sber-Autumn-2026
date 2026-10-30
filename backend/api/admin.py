"""Панель вовлечённости для роли admin: кто пользуется платформой и как глубоко.

Активность считается по дням, в которые пользователь обращался к API (user_activity_days),
действия — по уже существующим данным: диалоги, проекты, запуски, документы, одобрения.
Агрегаты собираются GROUP BY по владельцу, дневные ряды — в Python, чтобы не зависеть от
функций дат SQLite и Postgres.
"""

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.history import SessionDependency
from backend.auth import AdminUser
from backend.models import (
    Artifact,
    ArtifactApproval,
    Conversation,
    ConversationFile,
    Message,
    Project,
    ProjectFile,
    ProjectRun,
    Run,
    User,
    UserActivityDay,
)


router = APIRouter(prefix="/api/admin", tags=["admin"])

PeriodDays = Annotated[int, Query(ge=7, le=365)]


class Totals(BaseModel):
    users: int
    admins: int
    new_users: int
    active_1d: int
    active_7d: int
    active_30d: int
    active_period: int
    returning_period: int
    conversations: int
    projects: int
    runs: int
    failed_runs: int
    user_messages: int
    artifacts: int
    approvals: int
    files: int
    cost_usd: float
    compute_seconds: float


class DayStats(BaseModel):
    day: date
    active_users: int
    new_users: int
    runs: int
    user_messages: int


class FunnelStep(BaseModel):
    key: str
    label: str
    users: int


class Overview(BaseModel):
    generated_at: datetime
    period_days: int
    totals: Totals
    daily: list[DayStats]
    funnel: list[FunnelStep]


class UserEngagement(BaseModel):
    id: str
    email: str
    role: str
    created_at: datetime
    last_seen_at: datetime | None
    last_action_at: datetime | None
    active_days: int
    active_days_period: int
    conversations: int
    projects: int
    runs: int
    failed_runs: int
    user_messages: int
    artifacts: int
    approvals: int
    files: int
    cost_usd: float
    compute_seconds: float


def _utc(value: datetime | None) -> datetime | None:
    # SQLite возвращает время без зоны, хотя пишем мы его в UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _by_owner(session: AsyncSession, statement: Select[Any]) -> dict[str, int]:
    return {owner: int(value or 0) for owner, value in (await session.execute(statement)).all() if owner}


def _merge(*counts: dict[str, int]) -> dict[str, int]:
    merged: Counter[str] = Counter()
    for count in counts:
        merged.update(count)
    return dict(merged)


def _per_day(values: Iterable[datetime | None]) -> Counter[date]:
    return Counter(moment.date() for moment in map(_utc, values) if moment is not None)


async def _engagement(session: AsyncSession, period_start: date) -> list[UserEngagement]:
    runs = (
        await session.execute(
            select(
                Conversation.owner_id,
                func.count(Run.id),
                func.sum(case((Run.status.in_(("failed", "outcome_unknown")), 1), else_=0)),
                func.sum(Run.cost_usd),
                func.sum(Run.duration_ms),
            )
            .select_from(Run)
            .join(Conversation, Conversation.id == Run.conversation_id)
            .group_by(Conversation.owner_id)
        )
    ).all()
    run_stats = {owner: row for owner, *row in runs if owner}

    conversations = await _by_owner(
        session,
        select(Conversation.owner_id, func.count()).group_by(Conversation.owner_id),
    )
    projects = await _by_owner(
        session,
        select(Project.owner_id, func.count())
        .where(Project.example.is_(False), Project.deleted.is_(False))
        .group_by(Project.owner_id),
    )
    messages = await _by_owner(
        session,
        select(Conversation.owner_id, func.count())
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.role == "user")
        .group_by(Conversation.owner_id),
    )
    last_actions = dict(
        (
            await session.execute(
                select(Conversation.owner_id, func.max(Message.created_at))
                .select_from(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Message.role == "user")
                .group_by(Conversation.owner_id)
            )
        ).all()
    )
    artifacts = await _by_owner(
        session,
        select(Conversation.owner_id, func.count(Artifact.id))
        .select_from(Artifact)
        .join(Run, Run.id == Artifact.run_id)
        .join(Conversation, Conversation.id == Run.conversation_id)
        .group_by(Conversation.owner_id),
    )
    # Одобрения рабочего пространства (V2) и принятые результаты прежнего интерфейса.
    approvals = _merge(
        await _by_owner(
            session,
            select(Project.owner_id, func.count())
            .select_from(ArtifactApproval)
            .join(Project, Project.id == ArtifactApproval.project_id)
            .group_by(Project.owner_id),
        ),
        await _by_owner(
            session,
            select(Project.owner_id, func.count())
            .select_from(ProjectRun)
            .join(Project, Project.id == ProjectRun.project_id)
            .where(ProjectRun.accepted.is_(True))
            .group_by(Project.owner_id),
        ),
    )
    files = _merge(
        await _by_owner(
            session,
            select(Project.owner_id, func.count())
            .select_from(ProjectFile)
            .join(Project, Project.id == ProjectFile.project_id)
            .group_by(Project.owner_id),
        ),
        await _by_owner(
            session,
            select(Conversation.owner_id, func.count())
            .select_from(ConversationFile)
            .join(Conversation, Conversation.id == ConversationFile.conversation_id)
            .group_by(Conversation.owner_id),
        ),
    )
    active_days = await _by_owner(
        session,
        select(UserActivityDay.user_id, func.count()).group_by(UserActivityDay.user_id),
    )
    active_days_period = await _by_owner(
        session,
        select(UserActivityDay.user_id, func.count())
        .where(UserActivityDay.day >= period_start)
        .group_by(UserActivityDay.user_id),
    )

    rows = []
    for user in await session.scalars(select(User).order_by(User.created_at)):
        total_runs, failed, cost, duration_ms = run_stats.get(user.id, (0, 0, None, None))
        rows.append(
            UserEngagement(
                id=user.id,
                email=user.email,
                role=user.role,
                created_at=_utc(user.created_at),
                last_seen_at=_utc(user.last_seen_at),
                last_action_at=_utc(last_actions.get(user.id)),
                active_days=active_days.get(user.id, 0),
                active_days_period=active_days_period.get(user.id, 0),
                conversations=conversations.get(user.id, 0),
                projects=projects.get(user.id, 0),
                runs=int(total_runs or 0),
                failed_runs=int(failed or 0),
                user_messages=messages.get(user.id, 0),
                artifacts=artifacts.get(user.id, 0),
                approvals=approvals.get(user.id, 0),
                files=files.get(user.id, 0),
                cost_usd=float(cost or 0),
                compute_seconds=(duration_ms or 0) / 1000,
            )
        )
    return rows


async def _active_since(session: AsyncSession, start: date) -> int:
    return int(
        await session.scalar(
            select(func.count(func.distinct(UserActivityDay.user_id))).where(UserActivityDay.day >= start)
        )
        or 0
    )


@router.get("/overview", response_model=Overview)
async def overview(admin: AdminUser, session: SessionDependency, days: PeriodDays = 30) -> Overview:
    del admin
    now = datetime.now(UTC)
    today = now.date()
    period_start = today - timedelta(days=days - 1)
    start_moment = datetime.combine(period_start, datetime.min.time(), tzinfo=UTC)
    users = await _engagement(session, period_start)

    active = Counter(
        dict(
            (
                await session.execute(
                    select(UserActivityDay.day, func.count())
                    .where(UserActivityDay.day >= period_start)
                    .group_by(UserActivityDay.day)
                )
            ).all()
        )
    )
    new_users = _per_day(user.created_at for user in users)
    runs = _per_day(
        await session.scalars(
            select(func.coalesce(Run.started_at, Run.completed_at)).where(
                func.coalesce(Run.started_at, Run.completed_at) >= start_moment
            )
        )
    )
    messages = _per_day(
        await session.scalars(
            select(Message.created_at).where(Message.role == "user", Message.created_at >= start_moment)
        )
    )
    daily = [
        DayStats(
            day=day,
            active_users=active.get(day, 0),
            new_users=new_users.get(day, 0),
            runs=runs.get(day, 0),
            user_messages=messages.get(day, 0),
        )
        for day in (period_start + timedelta(days=offset) for offset in range(days))
    ]

    def total(field: str) -> Any:
        return sum(getattr(user, field) for user in users)

    totals = Totals(
        users=len(users),
        admins=sum(user.role == "admin" for user in users),
        new_users=sum(new_users.get(day.day, 0) for day in daily),
        active_1d=await _active_since(session, today),
        active_7d=await _active_since(session, today - timedelta(days=6)),
        active_30d=await _active_since(session, today - timedelta(days=29)),
        active_period=sum(user.active_days_period > 0 for user in users),
        returning_period=sum(user.active_days_period > 1 for user in users),
        conversations=total("conversations"),
        projects=total("projects"),
        runs=total("runs"),
        failed_runs=total("failed_runs"),
        user_messages=total("user_messages"),
        artifacts=total("artifacts"),
        approvals=total("approvals"),
        files=total("files"),
        cost_usd=round(total("cost_usd"), 6),
        compute_seconds=round(total("compute_seconds"), 3),
    )
    funnel = [
        FunnelStep(key="registered", label="Зарегистрировались", users=len(users)),
        FunnelStep(
            key="started",
            label="Начали проект или чат",
            users=sum(user.conversations > 0 or user.projects > 0 for user in users),
        ),
        FunnelStep(key="ran", label="Запустили AI-команду", users=sum(user.runs > 0 for user in users)),
        FunnelStep(key="result", label="Получили документы", users=sum(user.artifacts > 0 for user in users)),
        FunnelStep(key="approved", label="Одобрили результат", users=sum(user.approvals > 0 for user in users)),
    ]
    return Overview(generated_at=now, period_days=days, totals=totals, daily=daily, funnel=funnel)


@router.get("/users", response_model=list[UserEngagement])
async def users(admin: AdminUser, session: SessionDependency, days: PeriodDays = 30) -> list[UserEngagement]:
    del admin
    period_start = datetime.now(UTC).date() - timedelta(days=days - 1)
    return await _engagement(session, period_start)
