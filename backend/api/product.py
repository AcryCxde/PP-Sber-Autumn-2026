"""HTTP-контракт V2 рабочего пространства CoreTeams (web-product/docs/ctf-v2-contract.md).

Мутации идемпотентны по заголовку Idempotency-Key: повтор с тем же телом возвращает прежний
результат, с другим — 409 idempotency_conflict. Запуски Claude выполняются в фоне,
клиент опрашивает GET .../runs/{runId}.
"""

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Annotated, Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.history import SessionDependency
from backend.auth import CurrentUser
from backend.materials import MAX_FILE_BYTES, MaterialError, check_upload
from backend.models import (
    Artifact,
    ArtifactApproval,
    Conversation,
    ProductRun,
    Project,
    ProjectFile,
    ProjectRun,
    Run,
)
from backend.product import (
    ROLES,
    ProductError,
    Workspace,
    answer_content,
    document_path,
    document_zip,
    initial_prompt,
    load_workspace,
    now,
    receipt,
    request_hash,
    rework_prompt,
)
from backend.repository import RunAcceptanceError, accept_run_in_transaction
from backend.storage import StorageError


router = APIRouter(prefix="/api/v2", tags=["product"])

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=64)]
STAGE_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class StageBody(BaseModel):
    id: str = Field(pattern=STAGE_ID)
    title: str = Field(max_length=200)
    goal: str = Field(max_length=1000)


class PlanBody(BaseModel):
    """Проект, как его собрал пользователь: задача, ответы подготовки, команда и план."""

    model_config = ConfigDict(extra="ignore")

    clientProjectId: str = Field(min_length=1, max_length=64)
    title: str = Field(max_length=120)
    goal: str = Field(max_length=4000)
    context: str = Field(max_length=8000)
    scenario: str = Field(max_length=40)
    answers: list[Annotated[str, Field(max_length=2000)]] = Field(min_length=3, max_length=3)
    team: list[str] = Field(min_length=1, max_length=len(ROLES))
    teamNote: str = Field(max_length=3000)
    stages: list[StageBody] = Field(min_length=1, max_length=12)

    @field_validator("stages")
    @classmethod
    def unique_stages(cls, stages: list[StageBody]) -> list[StageBody]:
        if len({stage.id for stage in stages}) != len(stages):
            raise ValueError("Повторяющиеся ID этапов")
        return stages

    def plan(self) -> dict[str, Any]:
        return self.model_dump(exclude={"clientProjectId"})


class RunBody(PlanBody):
    stageId: str = Field(pattern=STAGE_ID)
    fileIds: list[str | None] = Field(default_factory=list, max_length=64)
    text: str | None = Field(default=None, max_length=3000)
    parentRunId: str | None = None
    sourceArtifactId: str | None = None
    documentId: str | None = None
    sourceVersion: int | None = None


class AnswerBody(BaseModel):
    questionId: str = Field(min_length=1, max_length=100)
    answers: dict[str, Annotated[str, Field(max_length=3000)]]


class ApproveBody(BaseModel):
    artifactId: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)


def parse(model: type[BaseModel], body: dict[str, Any]) -> Any:
    try:
        return model.model_validate(body)
    except ValidationError as error:
        raise ProductError(422, "invalid_request", error.errors()[0]["msg"]) from None


def check_roles(team: list[str]) -> None:
    unknown = [role for role in team if role not in ROLES]
    if unknown or len(set(team)) != len(team):
        raise ProductError(422, "unknown_role", "Неизвестная или повторяющаяся роль: " + ", ".join(unknown))


def require_key(key: str | None) -> str:
    if not key or not key.strip():
        raise ProductError(422, "idempotency_key_required", "Нужен заголовок Idempotency-Key")
    return key.strip()


async def require_project(session: AsyncSession, project_id: str, user: CurrentUser) -> Project:
    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.owner_id == user.id,
            Project.deleted.is_(False),
            Project.client_project_id.is_not(None),
        ).execution_options(populate_existing=True)
    )
    if project is None:
        raise ProductError(404, "project_not_found", "Проект не найден")
    return project


async def require_chain(session: AsyncSession, project: Project, run_id: str):
    workspace = await load_workspace(session, project)
    chain = workspace.chain(run_id)
    if chain is None:
        raise ProductError(404, "run_not_found", "Запуск не найден")
    return workspace, chain


async def material_names(session: AsyncSession, project: Project, file_ids: list[str | None]) -> list[str]:
    return list(await session.scalars(
        select(ProjectFile.name).where(
            ProjectFile.project_id == project.id,
            ProjectFile.id.in_([item for item in file_ids if item]),
        ).order_by(ProjectFile.created_at)
    ))


async def start(
    request: Request,
    session: AsyncSession,
    project: Project,
    product: ProductRun,
    *,
    client_request_id: str,
    content: str,
    display_text: str,
    kind: str,
    owner_id: str,
    request_digest: str | None = None,
) -> None:
    """Принимает запуск Claude в диалоге проекта, связывает его с Run и запускает в фоне."""
    # Отложенный импорт: main подключает этот router.
    from backend import main

    state = request.app.state
    # Reserve through the same limiter as websocket runs before any acceptance I/O.
    # A gated worker cannot execute until acceptance and the product link commit together.
    ready = asyncio.get_running_loop().create_future()

    async def deliver(event: Any, **fields: Any) -> None:
        del event, fields

    async def execute_when_committed() -> None:
        acceptance = await ready
        await main.execute_accepted_run(state, acceptance, content, deliver)

    if not await main.start_run_task(request, execute_when_committed()):
        raise ProductError(503, "gateway_busy", "Достигнут лимит одновременных запусков")
    try:
        conversation = await session.get(Conversation, project.conversation_id)
        if conversation is None or conversation.status == "deleted":
            raise ProductError(409, "conversation_not_found", "Диалог проекта не найден")
        context_mode = "reset" if conversation.context_state == "unavailable" and kind != "answer" else "resume"
        acceptance = await accept_run_in_transaction(
            session,
            conversation_id=project.conversation_id,
            client_request_id=client_request_id,
            content=content,
            requested_context_mode=context_mode,
            owner_id=owner_id,
        )
        if acceptance.idempotent_replay:
            return
        stages = [stage["id"] for stage in (project.product or {})["stages"]]
        session.add(ProjectRun(
            run_id=acceptance.run_id,
            project_id=project.id,
            stage=stages.index(product.stage_id),
            kind=kind,
            product_run_id=product.id,
            display_text=display_text,
            request_hash=request_digest,
        ))
        project.updated_at = now()
        await session.commit()
        # No await between commit acknowledgement and releasing the worker. If the
        # process dies here, startup reconciliation makes the linked run outcome_unknown.
        ready.set_result(acceptance)
    except RunAcceptanceError as error:
        await session.rollback()
        raise ProductError(409, error.code, error.message) from error
    except IntegrityError as error:
        await session.rollback()
        raise ProductError(409, "idempotency_conflict", "Запрос уже принят или диалог занят") from error
    finally:
        if not ready.done():
            ready.cancel()


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: dict[str, Any],
    session: SessionDependency,
    user: CurrentUser,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, str]:
    del idempotency_key  # повтор распознаётся по clientProjectId
    plan = parse(PlanBody, body)
    check_roles(plan.team)
    query = select(Project.id).where(
        Project.owner_id == user.id, Project.client_project_id == plan.clientProjectId
    )
    existing = await session.scalar(query)
    if existing is not None:
        return {"id": existing}
    conversation = Conversation(owner_id=user.id, title=plan.title.strip()[:200] or "Новый проект")
    session.add(conversation)
    await session.flush()
    project = Project(
        owner_id=user.id,
        conversation_id=conversation.id,
        client_project_id=plan.clientProjectId,
        product=plan.plan(),
        name=plan.title.strip()[:120],
        problem=plan.goal,
        context=plan.context,
    )
    session.add(project)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(query)
        if existing is None:
            raise
        return {"id": existing}
    return {"id": project.id}


def unique_name(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    path = PurePath(name)
    index = 2
    while f"{path.stem} ({index}){path.suffix}" in taken:
        index += 1
    return f"{path.stem} ({index}){path.suffix}"


@router.post("/projects/{project_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    project_id: str,
    session: SessionDependency,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    clientFileId: Annotated[str, Form(min_length=1, max_length=64)],
    sha256: Annotated[str, Form(pattern=r"^[0-9a-f]{64}$")],
) -> dict[str, str]:
    data = await file.read(MAX_FILE_BYTES + 1)
    project = await require_project(session, project_id, user)
    await session.execute(select(Project.id).where(Project.id == project.id).with_for_update())
    existing = await session.scalar(
        select(ProjectFile).where(
            ProjectFile.project_id == project.id, ProjectFile.client_file_id == clientFileId
        )
    )
    if existing is not None:
        if existing.sha256 != sha256:
            raise ProductError(409, "idempotency_conflict", "Файл с этим ID уже загружен с другим содержимым")
        return {"id": existing.id, "sha256": existing.sha256}
    if len(data) <= MAX_FILE_BYTES and hashlib.sha256(data).hexdigest() != sha256:
        raise ProductError(422, "sha256_mismatch", "Полученные байты не совпадают с SHA-256")
    files = list(await session.execute(
        select(ProjectFile.name, ProjectFile.size_bytes).where(ProjectFile.project_id == project.id)
    ))
    try:
        # Разные файлы с одинаковым именем не заменяют друг друга, как в прежнем интерфейсе.
        name = unique_name(check_upload(file.filename, data, []), {name for name, _ in files})
        check_upload(name, data, files)
    except MaterialError as error:
        raise ProductError(error.status_code, "file_rejected", str(error)) from None
    stored = ProjectFile(
        project_id=project.id,
        name=name,
        size_bytes=len(data),
        content=data,
        client_file_id=clientFileId,
        sha256=sha256,
    )
    session.add(stored)
    project.updated_at = now()
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # A competing request may already have persisted this exact receipt.
        existing = await session.scalar(select(ProjectFile).where(
            ProjectFile.project_id == project_id, ProjectFile.client_file_id == clientFileId
        ))
        if existing is None:
            raise
        if existing.sha256 != sha256:
            raise ProductError(409, "idempotency_conflict", "Файл с этим ID уже загружен с другим содержимым") from None
        return {"id": existing.id, "sha256": existing.sha256}
    return {"id": stored.id, "sha256": sha256}


@router.post("/projects/{project_id}/runs", status_code=status.HTTP_201_CREATED)
async def create_run(
    project_id: str,
    body: dict[str, Any],
    request: Request,
    response: Response,
    session: SessionDependency,
    user: CurrentUser,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    key = require_key(idempotency_key)
    project = await require_project(session, project_id, user)
    digest = request_hash(body)
    previous = await session.scalar(select(ProductRun).where(ProductRun.client_request_id == key))
    if previous is not None:
        if previous.project_id != project.id or previous.request_hash != digest:
            raise ProductError(409, "idempotency_conflict", "Idempotency-Key уже использован для другого запроса")
        response.status_code = status.HTTP_200_OK
        _, chain = await require_chain(session, project, previous.id)
        return chain.snapshot()
    run = parse(RunBody, body)
    source = await rework_source(request, session, project, run) if run.parentRunId else None
    if source is not None:
        await session.refresh(user)
    # Storage I/O above holds no transaction/row lock. Re-check all workspace
    # invariants under the root lock before accepting an execution.
    project = await require_project(session, project_id, user)
    await session.execute(select(Project.id).where(Project.id == project.id).with_for_update())
    digest = request_hash(body)
    previous = await session.scalar(select(ProductRun).where(ProductRun.client_request_id == key))
    if previous is not None:
        if previous.project_id != project.id or previous.request_hash != digest:
            raise ProductError(409, "idempotency_conflict", "Idempotency-Key уже использован для другого запроса")
        response.status_code = status.HTTP_200_OK
        _, chain = await require_chain(session, project, previous.id)
        return chain.snapshot()

    workspace = await load_workspace(session, project)
    if workspace.busy():
        raise ProductError(409, "project_state_conflict", "Предыдущий запуск ещё не завершён")
    file_ids = {item for item in run.fileIds if item}
    known = set(await session.scalars(select(ProjectFile.id).where(ProjectFile.project_id == project.id)))
    if not file_ids <= known:
        raise ProductError(409, "file_not_found", "Сервис не получил часть файлов проекта")

    if run.parentRunId is None:
        content, display = await prepare_initial(session, project, workspace, run)
        kind = "stage"
    else:
        content, display = await prepare_rework(session, project, workspace, run, source)
        kind = "revision"

    product = ProductRun(
        project_id=project.id,
        client_request_id=key,
        request_hash=digest,
        file_ids=sorted(file_ids),
        stage_id=run.stageId,
        parent_run_id=run.parentRunId,
        source_artifact_id=run.sourceArtifactId,
    )
    session.add(product)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ProductError(409, "idempotency_conflict", "Idempotency-Key уже использован") from None
    try:
        await start(
            request, session, project, product,
            client_request_id=product.id,
            content=content,
            display_text=display,
            kind=kind,
            owner_id=user.id,
        )
    except ProductError:
        await session.rollback()
        raise
    _, chain = await require_chain(session, project, product.id)
    return chain.snapshot()


async def prepare_initial(
    session: AsyncSession, project: Project, workspace: Workspace, run: RunBody
) -> tuple[str, str]:
    if workspace.chains:
        raise ProductError(409, "project_state_conflict", "Проект уже запущен; доработка — отдельным Run")
    if run.clientProjectId != project.client_project_id:
        raise ProductError(409, "project_mismatch", "Запуск относится к другому проекту")
    check_roles(run.team)
    if not run.goal.strip():
        raise ProductError(422, "project_incomplete", "Опишите задачу проекта")
    plan = run.plan()
    for extra in ("stageId", "fileIds", "text", "parentRunId", "sourceArtifactId", "documentId", "sourceVersion"):
        plan.pop(extra, None)
    if run.stageId not in {stage["id"] for stage in plan["stages"]}:
        raise ProductError(422, "unknown_stage", "Этап не входит в план проекта")
    # До первого запуска пользователь мог поправить задачу, команду и план.
    project.product = plan
    project.name = run.title.strip()[:120]
    project.problem = run.goal
    project.context = run.context
    project.status = "active"
    conversation = await session.get(Conversation, project.conversation_id)
    if conversation is not None and project.name:
        conversation.title = project.name[:200]
    content = initial_prompt(plan, await material_names(session, project, run.fileIds))
    display = run.goal.strip() if not run.title.strip() else f"{run.title.strip()}\n\n{run.goal.strip()}"
    return content, display


@dataclass(frozen=True)
class ReworkSource:
    artifact_id: int
    storage_key: str | None
    sha256: str
    size_bytes: int
    content: str


async def rework_source(request: Request, session: AsyncSession, project: Project, run: RunBody) -> ReworkSource:
    # Validate ownership and version before dereferencing an object-storage key.
    workspace = await load_workspace(session, project)
    document = workspace.document(run.sourceArtifactId or "")
    parent = workspace.chain(run.parentRunId or "")
    if parent is None or workspace.chains[-1] is not parent or parent.status() != "completed":
        raise ProductError(409, "project_state_conflict", "Доработка доступна только после завершённого запуска")
    if document is None or (run.stageId, run.documentId, run.sourceVersion) != (
        document.stage_id, document.document_id, document.version
    ):
        raise ProductError(409, "version_conflict", "Исходная версия не совпадает с сохранённой")
    artifact = await session.get(Artifact, int(document.id.removeprefix("a")))
    if artifact is None:
        raise ProductError(409, "version_conflict", "Исходный документ не найден")
    artifact_id, storage_key, digest, size = artifact.id, artifact.storage_key, artifact.sha256, artifact.size_bytes
    inline = artifact.content
    limit = request.app.state.settings.artifact_max_storage_bytes
    if not digest:
        raise ProductError(503, "source_unavailable", "У старого снимка нет контрольной суммы; создайте новую сохранённую версию документа")
    if size is None or size > limit:
        raise ProductError(503, "source_unavailable", "Полный исходный документ недоступен или превышает лимит")
    # Release the read transaction before storage I/O. No DB state has been mutated.
    await session.rollback()
    storage = getattr(request.app.state, "storage", None)
    if storage_key:
        if storage is None:
            raise ProductError(503, "source_unavailable", "Хранилище исходного документа недоступно")
        try:
            async with asyncio.timeout(30):
                data = await storage.get_bounded(storage_key, limit)
        except (StorageError, TimeoutError) as error:
            raise ProductError(503, "source_unavailable", "Не удалось прочитать исходный документ") from error
    else:
        data = inline.encode("utf-8") if inline is not None else b""
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest or b"\x00" in data:
        raise ProductError(503, "source_unavailable", "Полный исходный документ не прошёл проверку целостности")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProductError(409, "source_not_text", "Доработка доступна только для UTF-8 документов") from error
    return ReworkSource(artifact_id, storage_key, digest, size, content)


async def prepare_rework(
    session: AsyncSession, project: Project, workspace: Workspace, run: RunBody, source: ReworkSource | None
) -> tuple[str, str]:
    parent = workspace.chain(run.parentRunId or "")
    if parent is None or workspace.chains[-1] is not parent or parent.status() != "completed":
        raise ProductError(409, "project_state_conflict", "Доработка доступна только после завершённого запуска")
    document = workspace.document(run.sourceArtifactId or "")
    if document is None:
        raise ProductError(409, "version_conflict", "Исходная версия документа не найдена")
    if (
        run.stageId != document.stage_id
        or run.documentId != document.document_id
        or run.sourceVersion != document.version
    ):
        raise ProductError(409, "version_conflict", "Исходная версия не совпадает с сохранённой")
    artifact = await session.get(Artifact, int(document.id.removeprefix("a")), populate_existing=True)
    if source is None or artifact is None or (
        artifact.id, artifact.storage_key, artifact.sha256, artifact.size_bytes
    ) != (source.artifact_id, source.storage_key, source.sha256, source.size_bytes):
        raise ProductError(409, "version_conflict", "Исходный документ изменился во время чтения")
    text = (run.text or "").strip()
    if not text:
        raise ProductError(422, "invalid_request", "Опишите, что доработать")
    plan = project.product or {}
    stage = next(item for item in plan["stages"] if item["id"] == document.stage_id)
    content = rework_prompt(
        plan,
        await material_names(session, project, run.fileIds),
        stage=stage,
        path=document_path(document.document_id, plan["stages"]),
        version=document.version,
        content=source.content,
        text=text,
    )
    return content, text


@router.get("/projects/{project_id}/runs/{run_id}")
async def get_run(project_id: str, run_id: str, session: SessionDependency, user: CurrentUser) -> dict[str, Any]:
    project = await require_project(session, project_id, user)
    _, chain = await require_chain(session, project, run_id)
    return chain.snapshot()


@router.post("/projects/{project_id}/runs/{run_id}/answers")
async def answer(
    project_id: str,
    run_id: str,
    body: dict[str, Any],
    request: Request,
    session: SessionDependency,
    user: CurrentUser,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    key = require_key(idempotency_key)
    project = await require_project(session, project_id, user)
    # Serialize question validation and replay detection with the immutable link.
    await session.execute(select(Project.id).where(Project.id == project.id).with_for_update())
    _, chain = await require_chain(session, project, run_id)
    # Запуск Claude с этим ключом уже есть — это повтор после обрыва связи.
    client_request_id = str(uuid5(NAMESPACE_URL, f"coreteams:answer:{project.id}:{key}"))
    replay = await session.scalar(select(Run).where(Run.client_request_id == client_request_id))
    if replay is not None:
        link = next((link for link, run in chain.links if run.id == replay.id), None)
        if link is not None and link.request_hash is None:
            raise ProductError(409, "idempotency_conflict", "Старый запрос не содержит отпечатка тела; обновите состояние запуска")
        if link is None or link.request_hash != request_hash(body):
            raise ProductError(409, "idempotency_conflict", "Idempotency-Key уже использован для другого запроса")
        return chain.snapshot()

    data = parse(AnswerBody, body)
    question = chain.question()
    if chain.status() != "waiting_for_input" or question is None or question["id"] != data.questionId:
        raise ProductError(409, "unknown_fork", "Вопрос устарел или уже получил ответ")
    if set(data.answers) != {item["text"] for item in question["questions"]} or not all(
        value.strip() for value in data.answers.values()
    ):
        raise ProductError(422, "answers_mismatch", "Нужен ответ на каждый вопрос фасилитатора")
    content = answer_content(question["questions"], data.answers)
    await start(
        request, session, project, chain.product,
        client_request_id=client_request_id,
        content=content,
        display_text=content,
        kind="answer",
        owner_id=user.id,
        request_digest=request_hash(body),
    )
    _, chain = await require_chain(session, project, run_id)
    return chain.snapshot()


@router.post("/projects/{project_id}/runs/{run_id}/artifacts/{artifact_id}/approve")
async def approve(
    project_id: str,
    run_id: str,
    artifact_id: str,
    body: dict[str, Any],
    session: SessionDependency,
    user: CurrentUser,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    key = require_key(idempotency_key)
    project = await require_project(session, project_id, user)
    await session.execute(select(Project.id).where(Project.id == project.id).with_for_update())
    data = parse(ApproveBody, body)
    previous = await session.scalar(select(ArtifactApproval).where(ArtifactApproval.client_request_id == key))
    if previous is not None:
        if data.artifactId != artifact_id or (previous.project_id, previous.artifact_id, previous.version) != (project.id, artifact_id, data.version):
            raise ProductError(409, "idempotency_conflict", "Idempotency-Key уже использован для другого запроса")
        return receipt(previous)
    _, chain = await require_chain(session, project, run_id)
    document = next((item for item in chain.documents if item.id == artifact_id), None)
    if document is None or data.artifactId != artifact_id:
        raise ProductError(404, "artifact_not_found", "Версия документа не найдена в этом запуске")
    if document.version != data.version:
        raise ProductError(409, "version_conflict", "Одобряемая версия не совпадает с сохранённой")
    existing = await session.scalar(
        select(ArtifactApproval).where(
            ArtifactApproval.project_id == project.id, ArtifactApproval.artifact_id == artifact_id
        )
    )
    if existing is not None:
        return receipt(existing)
    approval = ArtifactApproval(
        id=str(uuid4()),
        project_id=project.id,
        product_run_id=chain.product.id,
        artifact_id=artifact_id,
        version=document.version,
        client_request_id=key,
        approved_at=now(),
    )
    session.add(approval)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # Global keys may race across different project roots. Never turn an
        # unrelated integrity failure into a successful approval receipt.
        previous = await session.scalar(select(ArtifactApproval).where(ArtifactApproval.client_request_id == key))
        if previous is None:
            raise
        if data.artifactId != artifact_id or (previous.project_id, previous.artifact_id, previous.version) != (
            project_id, artifact_id, data.version
        ):
            raise ProductError(409, "idempotency_conflict", "Idempotency-Key уже использован для другого запроса") from None
        return receipt(previous)
    return receipt(approval)


@router.get("/projects/{project_id}/runs/{run_id}/artifacts/{artifact_id}/zip")
async def download_zip(
    project_id: str,
    run_id: str,
    artifact_id: str,
    request: Request,
    session: SessionDependency,
    user: CurrentUser,
) -> Response:
    project = await require_project(session, project_id, user)
    _, chain = await require_chain(session, project, run_id)
    document = next((item for item in chain.documents if item.id == artifact_id), None)
    if document is None:
        raise ProductError(404, "artifact_not_found", "Версия документа не найдена в этом запуске")
    approval = await session.scalar(
        select(ArtifactApproval).where(
            ArtifactApproval.project_id == project.id, ArtifactApproval.artifact_id == artifact_id
        )
    )
    original = None
    stored = await session.get(Artifact, int(artifact_id.removeprefix("a")))
    storage = getattr(request.app.state, "storage", None)
    if stored is not None and stored.storage_key and storage is not None:
        try:
            original = await storage.get(stored.storage_key)
        except StorageError:
            raise ProductError(503, "storage_unavailable", "Хранилище файлов недоступно") from None
    name = re.sub(r"[^A-Za-z0-9_-]", "", document.document_id)[:40] or "document"
    return Response(
        document_zip(project, document, approval, original),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="coreteams-{name}-v{document.version}.zip"'},
    )
