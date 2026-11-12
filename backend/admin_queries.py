"""Bounded, single-statement operational snapshots; never expose user content."""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, and_, case, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Conversation, ProductRun, Project, ProjectRun, Run, User
from backend.harness_estimate import safe_estimate
from backend.public_events import SAFE_FAILURE_CODES, SAFE_UNKNOWN_CODES
from backend.scenarios import SCENARIO_KEYS, stages_for


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise ValueError("invalid_period")

    def wire(self):
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


def parse_period(start: str | None, end: str | None, now: datetime) -> Period:
    if (start is None) != (end is None):
        raise ValueError("invalid_period")
    if start is None:
        return Period(now - timedelta(days=7), now)
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if first.tzinfo is None or last.tzinfo is None:
            raise ValueError("invalid_period")
        return Period(first.astimezone(UTC), last.astimezone(UTC))
    except (ValueError, TypeError):
        raise ValueError("invalid_period") from None


def cohort(period: Period):
    return and_(Run.started_at >= period.start, Run.started_at < period.end)


def _stamp(value):
    return value.replace(tzinfo=UTC).isoformat() if value is not None and value.tzinfo is None else value.isoformat() if value else None


def _stage(row):
    index = row.stage
    if type(index) is not int or index < 0:
        return None
    if row.product_run_id is not None:
        stages = row.product.get("stages") if isinstance(row.product, dict) else None
        if not isinstance(stages, list) or index >= len(stages):
            return None
        item = stages[index]
        if not isinstance(item, dict) or item.get("id") != row.stage_id or not isinstance(item.get("title"), str):
            return None
        return {"index": index, "id": item["id"], "title": item["title"], "source": "product_stage"}
    if row.scenario not in SCENARIO_KEYS:
        return None
    stages = stages_for(row.scenario)
    if index >= len(stages):
        return None
    return {"index": index, "id": None, "title": stages[index].title, "source": "scenario_catalog"}


def _row(row):
    project_id = row.project_id
    return {"id": row.id, "conversation_id": row.conversation_id, "project_id": project_id,
        "task_label": f"Проект {project_id[:8]}" if project_id else f"Диалог {row.conversation_id[:8]}",
        "user": {"id": row.user_id, "email": row.email} if row.user_id else None,
        "status": row.status, "stage": _stage(row), "started_at": _stamp(row.started_at),
        "completed_at": _stamp(row.completed_at), "project_href": None,
        "error_code": row.error_code if row.error_code in SAFE_FAILURE_CODES | SAFE_UNKNOWN_CODES else "unclassified" if row.error_code else None}


def _details(statement):
    return statement.add_columns(Run.id, Run.conversation_id, Run.status, Run.started_at, Run.completed_at,
        Run.error_code, User.id.label("user_id"), User.email, ProjectRun.project_id, ProjectRun.stage,
        ProjectRun.product_run_id, ProductRun.stage_id, Project.scenario, Project.product).outerjoin(
        Run, Run.id == statement.selected_columns.run_id).outerjoin(Conversation, Conversation.id == Run.conversation_id
        ).outerjoin(User, User.id == Conversation.owner_id).outerjoin(ProjectRun, ProjectRun.run_id == Run.id
        ).outerjoin(Project, Project.id == ProjectRun.project_id).outerjoin(ProductRun, ProductRun.id == ProjectRun.product_run_id)


async def overview(session: AsyncSession, period: Period, attention_period: Period, *, active_page: int,
                   attention_page: int, page_size: int, attention_no_start: bool):
    active = select(Run.id.label("run_id")).where(Run.status.in_(["pending", "running"]))
    attention = select(Run.id.label("run_id")).where(Run.status.in_(["failed", "outcome_unknown"]),
        Run.started_at.is_(None) if attention_no_start else cohort(attention_period))
    counts = select(func.count().filter(cohort(period)).label("runs"),
        *[func.count().filter(and_(cohort(period), Run.status == status)).label(status)
          for status in ("failed", "outcome_unknown", "running")],
        func.count().filter(Run.started_at.is_(None)).label("without_started_at"),
        select(func.count()).select_from(active.subquery()).scalar_subquery().label("active_total"),
        select(func.count()).select_from(attention.subquery()).scalar_subquery().label("attention_total")).select_from(Run).cte("counts")
    active_ids = active.order_by(case((Run.status == "running", 0), else_=1), Run.started_at.asc().nullslast(), Run.id).limit(page_size).offset((active_page - 1) * page_size).subquery()
    attention_ids = attention.order_by(Run.completed_at.desc().nullslast(), Run.started_at.desc().nullslast(), Run.id).limit(page_size).offset((attention_page - 1) * page_size).subquery()
    selected = union_all(select(literal("meta").label("kind"), literal(None, String).label("run_id")),
        select(literal("active"), active_ids.c.run_id), select(literal("attention"), attention_ids.c.run_id)).cte("selected")
    statement = select(selected.c.kind, selected.c.run_id, *counts.c).select_from(selected).join(counts, literal(True))
    statement = _details(statement).order_by(selected.c.kind,
        case((selected.c.kind == "active", case((Run.status == "running", 0), else_=1))),
        case((selected.c.kind == "active", Run.started_at)).asc().nullslast(),
        case((selected.c.kind == "attention", Run.completed_at)).desc().nullslast(),
        case((selected.c.kind == "attention", Run.started_at)).desc().nullslast(), Run.id)
    rows = (await session.execute(statement)).all()
    meta = rows[0]
    def page(kind, number, total):
        return {"items": [_row(row) for row in rows if row.kind == kind], "total": total, "page": number, "page_size": page_size}
    return {"as_of": datetime.now(UTC).isoformat(), "period": period.wire(), "completeness": "complete",
        "totals": {key: getattr(meta, key) for key in ("runs", "failed", "outcome_unknown", "running")},
        "without_started_at": meta.without_started_at,
        "active": page("active", active_page, meta.active_total),
        "attention": {**page("attention", attention_page, meta.attention_total), "period": attention_period.wire(), "no_start": attention_no_start}}


async def runs(session: AsyncSession, period: Period, *, status: str, page: int, page_size: int):
    predicate = cohort(period) if status == "all" else and_(cohort(period), Run.status == status)
    ids = select(Run.id.label("run_id")).where(predicate).order_by(Run.started_at, Run.id).limit(page_size).offset((page - 1) * page_size).subquery()
    total = select(func.count()).select_from(Run).where(predicate).cte("total")
    selected = select(ids.c.run_id).cte("selected")
    statement = select(selected.c.run_id, total.c.count.label("total")).select_from(total).outerjoin(selected, literal(True))
    rows = (await session.execute(_details(statement).order_by(Run.started_at, Run.id))).all()
    return {"as_of": datetime.now(UTC).isoformat(), "period": period.wire(), "completeness": "complete",
            "items": [_row(row) for row in rows if row.id is not None], "total": rows[0].total,
            "page": page, "page_size": page_size}


async def run_detail(session: AsyncSession, run_id: str):
    selected = select(Run.id.label("run_id")).where(Run.id == run_id).cte("selected")
    statement = _details(select(selected.c.run_id).select_from(selected)).add_columns(Run.harness_estimate)
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None
    return {"as_of": datetime.now(UTC).isoformat(), "run": _row(row), "harness_estimate": safe_estimate(row.harness_estimate) or unknown_estimate()}


def unknown_estimate():
    return {"state": "unknown", "value_usd": None, "scope": "unknown", "session_ref": None,
            "segment_ref": None, "observed_at": None,
            "source": {"kind": "harness_result", "field": "total_cost_usd", "event_id": None,
                       "journal_seq": None, "runner_version": None},
            "baseline": None, "continuity": "unverified", "unavailable_reason": "scope_unconfirmed"}
