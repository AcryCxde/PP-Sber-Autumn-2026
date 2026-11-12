"""Панель вовлечённости для роли admin: кто пользуется платформой и как глубоко.

Активность считается по дням, в которые пользователь обращался к API (user_activity_days),
действия — по уже существующим данным: диалоги, проекты, запуски, документы, одобрения.
Агрегаты собираются GROUP BY по владельцу, дневные ряды — в Python, чтобы не зависеть от
функций дат SQLite и Postgres.
"""

import sqlite3

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ConfigDict, StrictBool, StrictInt, ValidationError

from backend.ai_access import AiAccessError, change_ai_access, policy_transaction
from backend import admin_queries
from backend.models import AiPolicyAudit
from pydantic import BaseModel
from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import DBAPIError

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


class PolicyRoute(APIRoute):
    """Keep validation input private only on the new admin policy boundary."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                locations = {item["loc"][1] for item in error.errors() if len(item["loc"]) > 1}
                code = "invalid_pagination" if locations & {"page", "page_size"} else "invalid_policy_request"
                return _policy_error(AiAccessError(code, 422, "Некорректные параметры запроса."))

        return validated


v1_router = APIRouter(prefix="/api/admin/v1", tags=["admin"], route_class=PolicyRoute)


class AiPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frozen: StrictBool
    reason: str | None = None
    expected_version: StrictInt


def _policy_error(error: AiAccessError) -> JSONResponse:
    body: dict[str, object] = {"code": error.code, "detail": error.message}
    if error.current is not None:
        body["current"] = error.current
    return JSONResponse(body, status_code=error.status_code, headers={"Cache-Control": "no-store"})


@v1_router.patch("/users/{user_id}/ai-access")
async def change_policy(user_id: str, request: Request, admin: AdminUser, session: SessionDependency):
    if not request.app.state.settings.admin_policy_management_enabled:
        return _policy_error(AiAccessError("policy_management_unavailable", 503, "Управление AI-доступом пока недоступно."))
    origin = request.headers.get("origin")
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    if (request.headers.get("x-coreteams-admin") != "1" or
            (origin is not None and origin != expected_origin) or
            request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json"):
        return _policy_error(AiAccessError("invalid_admin_origin", 403, "Недопустимый источник изменения."))
    try:
        raw = await request.body()
        if len(raw) > 16384:
            raise AiAccessError("invalid_policy_request", 422, "Некорректный запрос.")
        # Explicit parsing prevents FastAPI's validation response echoing private input.
        payload = AiPolicyRequest.model_validate_json(raw)
        key = request.headers.get("idempotency-key", "")
        actor_id = admin.id
        result = await policy_transaction(session, lambda: change_ai_access(
            session, actor_id=actor_id, target_id=user_id, frozen=payload.frozen,
            reason=payload.reason, expected_version=payload.expected_version,
            idempotency_key=key))
        return JSONResponse(result.response, headers={"Cache-Control": "no-store"})
    except ValidationError:
        return _policy_error(AiAccessError("invalid_policy_request", 422, "Некорректный запрос."))
    except AiAccessError as error:
        return _policy_error(error)


@v1_router.get("/users")
async def policy_users(admin: AdminUser, session: SessionDependency,
                       q: Annotated[str, Query(max_length=254)] = "",
                       page: Annotated[int, Query(ge=1)] = 1,
                       page_size: Annotated[int, Query(ge=1, le=100)] = 25):
    del admin
    predicate = User.email.icontains(q.strip(), autoescape=True)
    total = await session.scalar(select(func.count()).select_from(User).where(predicate))
    rows = await session.scalars(select(User).where(predicate)
        .order_by(User.created_at.desc(), User.id.desc()).limit(page_size).offset((page - 1) * page_size))
    return JSONResponse({"as_of": datetime.now(UTC).isoformat(), "items": [
        {"id": user.id, "email": user.email, "created_at": user.created_at.isoformat(),
         "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
         "ai_access": {"frozen": user.ai_frozen, "version": user.ai_policy_version}} for user in rows],
        "total": total, "page": page, "page_size": page_size}, headers={"Cache-Control": "no-store"})


@v1_router.get("/users/{user_id}/audit")
async def policy_audit(user_id: str, admin: AdminUser, session: SessionDependency,
                       page: Annotated[int, Query(ge=1)] = 1,
                       page_size: Annotated[int, Query(ge=1, le=100)] = 25):
    del admin
    if await session.get(User, user_id) is None:
        return _policy_error(AiAccessError("user_not_found", 404, "Пользователь не найден."))
    total = await session.scalar(select(func.count()).select_from(AiPolicyAudit).where(AiPolicyAudit.target_id == user_id))
    rows = await session.scalars(select(AiPolicyAudit).where(AiPolicyAudit.target_id == user_id)
        .order_by(AiPolicyAudit.created_at.desc(), AiPolicyAudit.id.desc()).limit(page_size).offset((page - 1) * page_size))
    return JSONResponse({"as_of": datetime.now(UTC).isoformat(), "items": [
        {"id": row.id, "actor_id": row.actor_id, "target_id": row.target_id,
         "created_at": row.created_at.isoformat(), "action": row.action, "reason": row.reason,
         "before_version": row.before_version, "after_version": row.after_version} for row in rows],
        "total": total, "page": page, "page_size": page_size}, headers={"Cache-Control": "no-store"})


@v1_router.get("/overview")
async def operational_overview(admin: AdminUser, session: SessionDependency,
        start: str | None = None, end: str | None = None,
        attention_start: str | None = None, attention_end: str | None = None,
        active_page: Annotated[int, Query(ge=1)] = 1,
        attention_page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25, attention_no_start: bool = False):
    del admin
    now = datetime.now(UTC)
    try:
        period = admin_queries.parse_period(start, end, now)
        attention_period = admin_queries.parse_period(attention_start, attention_end, now)
    except ValueError:
        return _policy_error(AiAccessError("invalid_period", 422, "Некорректный период."))
    body = await admin_queries.overview(session, period, attention_period, active_page=active_page,
        attention_page=attention_page, page_size=page_size, attention_no_start=attention_no_start)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@v1_router.get("/runs")
async def operational_runs(admin: AdminUser, session: SessionDependency,
        start: str | None = None, end: str | None = None, status: str = "all",
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25):
    del admin
    try:
        period = admin_queries.parse_period(start, end, datetime.now(UTC))
        if status not in {"all", "failed", "outcome_unknown", "running"}:
            raise ValueError
    except ValueError:
        return _policy_error(AiAccessError("invalid_period", 422, "Некорректная выборка."))
    return JSONResponse(await admin_queries.runs(session, period, status=status, page=page, page_size=page_size),
                        headers={"Cache-Control": "no-store"})


@v1_router.get("/runs/{run_id}")
async def operational_detail(run_id: str, request: Request, admin: AdminUser, session: SessionDependency):
    del admin
    transport = getattr(request.app.state, "runner_transport", None)
    projection_unavailable = False
    if transport is not None:
        from backend.harness_estimate import catch_up
        try:
            await catch_up(request.app.state.database, run_id, transport.diagnostic_snapshots(run_id))
        except (DBAPIError, sqlite3.Error):
            # Transport uses sqlite3 directly; catch_up owns a separate session.
            projection_unavailable = True
        except ValueError as error:
            if str(error) not in {"harness_lineage_conflict", "harness_observation_conflict", "invalid_harness_snapshot"}:
                raise
            projection_unavailable = True
    body = await admin_queries.run_detail(session, run_id)
    if body is None:
        return _policy_error(AiAccessError("run_not_found", 404, "Запуск не найден."))
    if projection_unavailable:
        body["harness_estimate"] = admin_queries.unknown_estimate()
        body["harness_estimate"]["unavailable_reason"] = "projection_unavailable"
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
