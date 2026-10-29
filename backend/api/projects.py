from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.history import SessionDependency
from backend.auth import CurrentUser
from backend.materials import (
    MAX_FILE_BYTES,
    MAX_PROJECT_BYTES,
    MAX_PROJECT_FILES,
    MaterialError,
    safe_file_name,
)
from backend.models import Conversation, Project, ProjectFile
from backend.projects import (
    ProjectRunError,
    approve,
    create_project,
    list_projects,
    owned_project,
    project_detail,
    project_summary,
    scenarios_catalog,
    stage_runs,
)
from backend.repository import delete_conversation
from backend.scenarios import SCENARIO_KEYS


router = APIRouter(prefix="/api", tags=["projects"])

SetupStep = Literal["welcome", "context", "question", "team-plan", "plan"]
Feedback = Literal["", "yes", "partly", "no"]


class ProjectCreate(BaseModel):
    example: bool = False


class ProjectUpdate(BaseModel):
    scenario: str | None = None
    name: str | None = Field(default=None, max_length=120)
    problem: str | None = Field(default=None, max_length=4000)
    context: str | None = Field(default=None, max_length=8000)
    answers: list[list[Annotated[str, Field(max_length=200)]]] | None = Field(
        default=None, min_length=3, max_length=3
    )
    custom: list[Annotated[str, Field(max_length=1000)]] | None = Field(
        default=None, min_length=3, max_length=3
    )
    team_feedback: str | None = Field(default=None, max_length=3000)
    note: str | None = Field(default=None, min_length=1, max_length=3000)
    setup_step: SetupStep | None = None
    question_index: int | None = Field(default=None, ge=0, le=2)
    feedback: Feedback | None = None
    feedback_note: str | None = Field(default=None, max_length=2000)


# Задача и направление определяют весь план: после запуска меняются только дополнения.
DRAFT_ONLY_FIELDS = {"scenario", "name", "problem", "answers", "custom", "setup_step", "question_index"}


async def require_project(session: AsyncSession, project_id: str, user: CurrentUser) -> Project:
    project = await owned_project(session, project_id, user.id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def detail(session: AsyncSession, project: Project) -> dict[str, Any]:
    return project_detail(project, await stage_runs(session, project))


def conflict(error: ProjectRunError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.message)


@router.get("/scenarios")
async def get_scenarios(user: CurrentUser) -> dict[str, Any]:
    del user
    return scenarios_catalog()


@router.get("/projects")
async def get_projects(session: SessionDependency, user: CurrentUser) -> list[dict[str, Any]]:
    projects = await list_projects(session, user.id)
    return [project_summary(item, await stage_runs(session, item)) for item in projects]


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def post_project(
    body: ProjectCreate,
    session: SessionDependency,
    user: CurrentUser,
) -> dict[str, Any]:
    created = await create_project(session, user.id, example=body.example)
    return await detail(session, await require_project(session, created.id, user))


@router.get("/projects/{project_id}")
async def get_project(project_id: str, session: SessionDependency, user: CurrentUser) -> dict[str, Any]:
    return await detail(session, await require_project(session, project_id, user))


@router.patch("/projects/{project_id}")
async def patch_project(
    project_id: str,
    body: ProjectUpdate,
    session: SessionDependency,
    user: CurrentUser,
) -> dict[str, Any]:
    project = await require_project(session, project_id, user)
    changes = body.model_dump(exclude_unset=True)
    if project.status != "draft" and DRAFT_ONLY_FIELDS & changes.keys():
        raise HTTPException(status_code=409, detail="Задача уже передана команде")
    if "scenario" in changes and changes["scenario"] not in SCENARIO_KEYS:
        raise HTTPException(status_code=422, detail="Unknown scenario")
    note = changes.pop("note", None)
    if note is not None:
        project.notes = [*project.notes, note.strip()][-20:]
    for key, value in changes.items():
        setattr(project, key, value)
    if changes.get("name", "").strip():
        conversation = await session.get(Conversation, project.conversation_id)
        if conversation is not None:
            conversation.title = changes["name"].strip()[:200]
    project.updated_at = datetime.now(UTC)
    await session.commit()
    return await detail(session, await require_project(session, project_id, user))


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_project(
    project_id: str,
    request: Request,
    session: SessionDependency,
    user: CurrentUser,
) -> Response:
    project = await require_project(session, project_id, user)
    outcome = await delete_conversation(session, project.conversation_id)
    if outcome == "busy":
        raise HTTPException(status_code=409, detail="Команда ещё работает")
    project.deleted = True
    await session.commit()
    # Как при удалении диалога: контейнер runner и тома сессии уходят вместе с проектом.
    runner_manager = getattr(request.app.state, "runner_manager", None)
    if runner_manager is not None:
        await runner_manager.discard(
            runner_manager.project_id(project.conversation_id),
            reason="project_deleted",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/projects/{project_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    project_id: str,
    session: SessionDependency,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    project = await require_project(session, project_id, user)
    try:
        name = safe_file_name(file.filename)
    except MaterialError as error:
        raise HTTPException(status_code=415, detail=str(error)) from None
    # Читаем на байт больше лимита, чтобы не держать в памяти слишком большой файл.
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Файл больше 5 МБ")
    if not data:
        raise HTTPException(status_code=422, detail="Пустой файл")
    count, total = (
        await session.execute(
            select(func.count(ProjectFile.id), func.coalesce(func.sum(ProjectFile.size_bytes), 0))
            .where(ProjectFile.project_id == project.id)
        )
    ).one()
    existing = next((item for item in project.files if item.name == name), None)
    if existing is None and count >= MAX_PROJECT_FILES:
        raise HTTPException(status_code=413, detail="В проекте уже 20 файлов")
    if total - (existing.size_bytes if existing else 0) + len(data) > MAX_PROJECT_BYTES:
        raise HTTPException(status_code=413, detail="Материалы проекта больше 20 МБ")
    if existing is not None:
        # Повторная загрузка файла с тем же именем заменяет его.
        await session.delete(existing)
        await session.flush()
    session.add(ProjectFile(project_id=project.id, name=name, size_bytes=len(data), content=data))
    project.updated_at = datetime.now(UTC)
    await session.commit()
    return await detail(session, await require_project(session, project_id, user))


@router.delete("/projects/{project_id}/files/{file_id}")
async def delete_file(
    project_id: str,
    file_id: str,
    session: SessionDependency,
    user: CurrentUser,
) -> dict[str, Any]:
    project = await require_project(session, project_id, user)
    existing = next((item for item in project.files if item.id == file_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="File not found")
    await session.delete(existing)
    await session.commit()
    return await detail(session, await require_project(session, project_id, user))


@router.post("/projects/{project_id}/approve")
async def approve_result(project_id: str, session: SessionDependency, user: CurrentUser) -> dict[str, Any]:
    project = await require_project(session, project_id, user)
    try:
        await approve(session, project)
    except ProjectRunError as error:
        raise conflict(error) from None
    return await detail(session, await require_project(session, project_id, user))


@router.post("/projects/{project_id}/finish")
async def finish_project(project_id: str, session: SessionDependency, user: CurrentUser) -> dict[str, Any]:
    project = await require_project(session, project_id, user)
    if project.status not in {"ready", "done"}:
        raise HTTPException(status_code=409, detail="Сначала примите все результаты")
    project.status = "done"
    await session.commit()
    return await detail(session, await require_project(session, project_id, user))


@router.post("/projects/{project_id}/reopen")
async def reopen_project(project_id: str, session: SessionDependency, user: CurrentUser) -> dict[str, Any]:
    project = await require_project(session, project_id, user)
    if project.status not in {"ready", "done"}:
        raise HTTPException(status_code=409, detail="Проект ещё в работе")
    project.status = "active"
    await session.commit()
    return await detail(session, await require_project(session, project_id, user))
