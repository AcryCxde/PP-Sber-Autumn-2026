"""Проекты CoreTeams: подготовка задачи, этапы и версии результатов.

Состояние этапа не хранится отдельно, а выводится из его последнего запуска:
идёт запуск → working, запуск закончился вопросом агента → waiting, завершился → review,
упал → failed, запусков ещё нет → idle. Так статус переживает перезапуск backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.models import Conversation, Event, Message, Project, ProjectFile, ProjectRun, Run
from backend.public_events import project_public_event_data
from backend.repository import ACTIVE_RUN_STATUSES
from backend.scenarios import (
    EXAMPLE_PROJECT,
    PromptInput,
    build_stage_prompt,
    catalog,
    scenario_of,
    stages_for,
    team_for,
)


RUN_KINDS = {"stage", "revision", "answer"}


class ProjectRunError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StageRun:
    link: ProjectRun
    run: Run
    content: str | None
    question: list[dict[str, Any]] | None


@dataclass(frozen=True)
class PreparedRun:
    conversation_id: str
    content: str
    context_mode: str
    stage: int


async def create_project(session: AsyncSession, owner_id: str, *, example: bool) -> Project:
    data = EXAMPLE_PROJECT if example else {}
    name = data.get("name", "")
    conversation = Conversation(owner_id=owner_id, title=name or "Новый проект")
    session.add(conversation)
    await session.flush()
    project = Project(
        owner_id=owner_id,
        conversation_id=conversation.id,
        example=example,
        scenario=data.get("scenario", "other"),
        name=name,
        problem=data.get("problem", ""),
        context=data.get("context", ""),
    )
    session.add(project)
    await session.commit()
    return project


async def owned_project(session: AsyncSession, project_id: str, owner_id: str) -> Project | None:
    return await session.scalar(
        select(Project)
        .where(
            Project.id == project_id,
            Project.owner_id == owner_id,
            Project.deleted.is_(False),
        )
        # Содержимое файлов не нужно для представления проекта.
        .options(selectinload(Project.files).load_only(
            ProjectFile.id, ProjectFile.name, ProjectFile.size_bytes, ProjectFile.created_at,
        ))
        .execution_options(populate_existing=True)
    )


async def list_projects(session: AsyncSession, owner_id: str) -> list[Project]:
    result = await session.scalars(
        select(Project)
        .where(Project.owner_id == owner_id, Project.deleted.is_(False))
        .order_by(Project.updated_at.desc())
    )
    return list(result)


async def stage_runs(session: AsyncSession, project: Project) -> list[StageRun]:
    rows = (
        await session.execute(
            select(ProjectRun, Run)
            .join(Run, Run.id == ProjectRun.run_id)
            .where(ProjectRun.project_id == project.id)
            .order_by(ProjectRun.created_at.asc())
        )
    ).all()
    run_ids = [run.id for _, run in rows]
    # Документ этапа — последнее сообщение запуска: до него Claude пишет промежуточные реплики.
    contents: dict[str, str] = {}
    questions: dict[str, list[dict[str, Any]]] = {}
    if run_ids:
        for event in await session.scalars(
            select(Event)
            .where(Event.run_id.in_(run_ids), Event.type.in_({"message.created", "question.asked"}))
            .order_by(Event.run_id, Event.seq)
        ):
            projected = project_public_event_data({**event.payload, "type": event.type})
            if event.type == "question.asked" and "questions" in projected:
                questions[event.run_id] = projected["questions"]
            elif event.type == "message.created" and projected.get("content", "").strip():
                contents[event.run_id] = projected["content"].strip()
        # Запуски без событий сообщений (старый формат) — по итоговому ответу ассистента.
        missing = [run_id for run_id in run_ids if run_id not in contents]
        if missing:
            contents.update(
                (
                    await session.execute(
                        select(Message.run_id, Message.content).where(
                            Message.run_id.in_(missing), Message.role == "assistant"
                        )
                    )
                ).all()
            )
    return [
        StageRun(link, run, contents.get(run.id), questions.get(run.id))
        for link, run in rows
    ]


def stage_results(runs: list[StageRun], stage: int) -> list[StageRun]:
    """Версии результата этапа: завершённые запуски с текстом, кроме тех, что закончились вопросом."""
    return [
        item for item in runs
        if item.link.stage == stage
        and item.run.status == "completed"
        and item.question is None
        and item.content
    ]


def stage_phase(project: Project, runs: list[StageRun]) -> tuple[str, StageRun | None]:
    if project.status in {"draft", "ready", "done"}:
        return project.status, None
    current = [item for item in runs if item.link.stage == project.stage]
    if not current:
        return "idle", None
    last = current[-1]
    if last.run.status in ACTIVE_RUN_STATUSES:
        return "working", last
    if last.run.status == "completed":
        return ("waiting" if last.question is not None else "review"), last
    return "failed", last


def _cost(runs: list[StageRun]) -> tuple[float, int]:
    cost = sum((item.run.cost_usd or Decimal(0) for item in runs), Decimal(0))
    duration = sum(item.run.duration_ms or 0 for item in runs)
    return float(cost), duration


def project_summary(project: Project, runs: list[StageRun]) -> dict[str, Any]:
    phase, _ = stage_phase(project, runs)
    stage = stages_for(project.scenario)[project.stage]
    return {
        "id": project.id,
        "name": project.name,
        "scenario": project.scenario,
        "example": project.example,
        "status": project.status,
        "phase": phase,
        "stage": project.stage,
        "stage_title": stage.title,
        "setup_step": project.setup_step,
        "updated_at": project.updated_at,
    }


def project_detail(project: Project, runs: list[StageRun]) -> dict[str, Any]:
    phase, last = stage_phase(project, runs)
    cost_usd, duration_ms = _cost(runs)
    results = []
    for stage in range(3):
        versions = stage_results(runs, stage)
        results.append([
            {
                "version": index + 1,
                "run_id": item.run.id,
                "content": item.content,
                "accepted": item.link.accepted,
                "created_at": item.run.completed_at or item.link.created_at,
            }
            for index, item in enumerate(versions)
        ])
    return {
        **project_summary(project, runs),
        "conversation_id": project.conversation_id,
        "problem": project.problem,
        "context": project.context,
        "answers": project.answers,
        "custom": project.custom,
        "team_feedback": project.team_feedback,
        "notes": project.notes,
        "question_index": project.question_index,
        "feedback": project.feedback,
        "feedback_note": project.feedback_note,
        "team": [role.__dict__ for role in team_for(project.scenario, project.answers)],
        "results": results,
        "files": [
            {"id": item.id, "name": item.name, "size_bytes": item.size_bytes}
            for item in project.files
        ],
        "active_run_id": last.run.id if phase == "working" and last else None,
        "question": last.question if phase == "waiting" and last else None,
        "error_code": last.run.error_code if phase == "failed" and last else None,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "runs_count": len(runs),
        "created_at": project.created_at,
    }


def _latest(versions: list[StageRun]) -> str | None:
    return versions[-1].content if versions else None


async def prepare_run(
    session: AsyncSession,
    project: Project,
    *,
    kind: str,
    text: str | None,
) -> PreparedRun:
    """Проверяет допустимость перехода и собирает текст запроса к Claude."""
    if kind not in RUN_KINDS:
        raise ProjectRunError("invalid_command", "Некорректный тип запуска проекта.")
    runs = await stage_runs(session, project)
    phase, _ = stage_phase(project, runs)
    text = (text or "").strip()

    if kind == "stage" and phase not in {"draft", "idle", "failed"}:
        raise ProjectRunError("project_state_conflict", "Этап уже начат.")
    if kind == "revision" and (phase != "review" or not text):
        raise ProjectRunError("project_state_conflict", "Доработка доступна только для готового результата.")
    if kind == "answer" and (phase != "waiting" or not text):
        raise ProjectRunError("project_state_conflict", "Сейчас команда не ждёт ответа.")
    if phase == "draft" and (not project.name.strip() or not project.problem.strip()):
        raise ProjectRunError("project_incomplete", "Укажите название и задачу проекта.")

    conversation = await session.get(Conversation, project.conversation_id)
    if conversation is None or conversation.status == "deleted":
        raise ProjectRunError("conversation_not_found", "Диалог проекта не найден.")

    if phase == "draft":
        project.status = "active"
        project.stage = 0
        project.updated_at = datetime.now(UTC)
        await session.commit()

    if kind == "answer":
        # Runner сам передаёт следующее сообщение диалога ответом на открытый вопрос.
        content = text
    else:
        previous = None
        if project.stage > 0:
            accepted = [item for item in stage_results(runs, project.stage - 1) if item.link.accepted]
            previous = _latest(accepted)
        content = build_stage_prompt(
            PromptInput(
                scenario=scenario_of(project.scenario),
                name=project.name,
                problem=project.problem,
                context=project.context,
                answers=project.answers,
                custom=project.custom,
                team_feedback=project.team_feedback,
                notes=project.notes,
                material_names=[item.name for item in project.files],
                previous_result=previous,
                current_result=_latest(stage_results(runs, project.stage)),
                revision=text if kind == "revision" else None,
            ),
            project.stage,
            kind,
        )
    # Промпт этапа самодостаточен: если контекст Claude потерян, начинаем с явного сброса.
    context_mode = "reset" if conversation.context_state == "unavailable" and kind != "answer" else "resume"
    return PreparedRun(conversation.id, content, context_mode, project.stage)


async def link_run(session: AsyncSession, project_id: str, run_id: str, stage: int, kind: str) -> None:
    if await session.get(ProjectRun, run_id) is None:
        session.add(ProjectRun(run_id=run_id, project_id=project_id, stage=stage, kind=kind))
        project = await session.get(Project, project_id)
        if project is not None:
            project.updated_at = datetime.now(UTC)
        await session.commit()


async def approve(session: AsyncSession, project: Project) -> None:
    runs = await stage_runs(session, project)
    phase, _ = stage_phase(project, runs)
    versions = stage_results(runs, project.stage)
    if phase != "review" or not versions:
        raise ProjectRunError("project_state_conflict", "Нет результата для одобрения.")
    versions[-1].link.accepted = True
    if project.stage >= 2:
        project.status = "ready"
    else:
        project.stage += 1
    project.updated_at = datetime.now(UTC)
    await session.commit()


def scenarios_catalog() -> dict[str, Any]:
    return catalog()
