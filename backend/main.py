import asyncio
import json
import logging
import subprocess
import weakref
from collections.abc import AsyncIterator
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from backend import models  # noqa: F401
from backend.api.history import router as history_router
from backend.api.projects_adapter import router as projects_adapter_router
from backend.config import PROJECT_ROOT, Settings
from backend.containers import ContainerError, ContainerManager
from backend.db import Database
from backend.models import Event
from backend.parser import (
    NormalizedEvent,
    bounded_file_content,
    normalized_event,
    parse_event,
)
from backend.public_events import message_created_event, project_public_event_data
from backend.repository import (
    TERMINAL_RUN_STATUSES,
    RunAcceptance,
    RunAcceptanceError,
    accept_run,
    append_artifact,
    append_event,
    client_request_exists,
    finalize_run,
    list_run_events,
    mark_run_running,
    reconcile_orphaned_runs,
)
from backend.runner_app import RunnerBridge, create_runner_router
from backend.runner_executor import run_via_runner
from backend.runner_transport import RunnerTransport
from backend.workspace import (
    WorkspaceError,
    acquire_gateway_lock,
    conversation_workspace,
    gateway_lock_path,
    normalize_artifact_path,
    release_gateway_lock,
    validate_workspace_root,
)


APP_SETTINGS = Settings()
logger = logging.getLogger(__name__)
# uvicorn настраивает только свои логгеры, а alembic.ini ставит корневому WARN: без собственного
# уровня не видно создания и удаления runner-контейнеров.
logging.getLogger("backend").setLevel(logging.INFO)
SESSION_UNAVAILABLE_MARKERS = (
    "no conversation found",
    "unable to resume",
    "session not found",
)


class ClaudeSessionMismatchError(RuntimeError):
    pass


class ArtifactSaveError(RuntimeError):
    pass


def write_prompt(process: subprocess.Popen[str], prompt: str) -> None:
    if process.stdin is None:
        process.terminate()
        raise RuntimeError("Claude CLI stdin is unavailable")

    process.stdin.write(prompt)
    process.stdin.close()


async def start_claude(
    prompt: str,
    workspace: str,
    resume_session_id: str | None = None,
    *,
    permission_mode: str | None = None,
) -> subprocess.Popen[str]:
    arguments = [
        "claude",
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--permission-mode",
        permission_mode or APP_SETTINGS.claude_permission_mode,
    ]
    if resume_session_id:
        arguments.extend(["--resume", resume_session_id])

    process = await asyncio.to_thread(
        subprocess.Popen,
        arguments,
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    await asyncio.to_thread(write_prompt, process, prompt)
    return process


async def process_output(
    process: subprocess.Popen[str],
) -> AsyncIterator[str]:
    if process.stdout is None:
        raise RuntimeError("Claude CLI stdout is unavailable")

    while line := await asyncio.to_thread(process.stdout.readline):
        yield line


async def drain_stderr(
    process: subprocess.Popen[str],
    *,
    run_id: str,
) -> bool:
    stderr = getattr(process, "stderr", None)
    if stderr is None:
        return False

    emitted_output = False
    session_unavailable = False
    while line := await asyncio.to_thread(stderr.readline):
        normalized = line.lower()
        if line.strip():
            emitted_output = True
        if any(marker in normalized for marker in SESSION_UNAVAILABLE_MARKERS):
            session_unavailable = True
    if emitted_output:
        logger.warning("Claude CLI emitted stderr for run %s", run_id)
    return session_unavailable


async def wait_process(process: subprocess.Popen[str]) -> int:
    return await asyncio.to_thread(process.wait)


async def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return

    process.terminate()
    try:
        await asyncio.to_thread(process.wait, 5)
    except subprocess.TimeoutExpired:
        process.kill()
        await asyncio.to_thread(process.wait)


def websocket_event(event: Event, conversation_id: str) -> dict[str, Any]:
    projected = project_public_event_data({**event.payload, "type": event.type})
    return {
        **projected,
        "conversation_id": conversation_id,
        "run_id": event.run_id,
        "seq": event.seq,
    }


def websocket_send_lock(websocket: WebSocket) -> asyncio.Lock:
    lock = getattr(websocket, "_gateway_send_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(websocket, "_gateway_send_lock", lock)
    return lock


async def send_websocket_payload(
    websocket: WebSocket,
    payload: dict[str, Any],
) -> None:
    async with websocket_send_lock(websocket):
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))


async def send_stored_event(
    websocket: WebSocket,
    event: Event,
    conversation_id: str,
    **wire_fields: Any,
) -> None:
    payload = websocket_event(event, conversation_id)
    payload.update(wire_fields)
    await send_websocket_payload(websocket, payload)


async def send_command_error(
    websocket: WebSocket,
    *,
    code: str,
    message: str,
    client_request_id: str | None = None,
    conversation_id: str | None = None,
    retryable: bool = False,
) -> None:
    await send_websocket_payload(
        websocket,
        {
            "type": "command.error",
            "request_type": "run.create",
            "client_request_id": client_request_id,
            "conversation_id": conversation_id,
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    )


def classify_session_unavailable(
    acceptance: RunAcceptance,
    *,
    exit_code: int | None,
    observed_session_id: str | None,
    result_text: str,
) -> str | None:
    if (
        acceptance.context_mode != "resume"
        or not acceptance.resume_session_id
        or observed_session_id
        or exit_code == 0
    ):
        return None

    normalized = result_text.lower()
    if any(marker in normalized for marker in SESSION_UNAVAILABLE_MARKERS):
        return "claude_session_unavailable"
    return None


async def handle_run_create(
    websocket: WebSocket,
    *,
    conversation_id: str | None,
    client_request_id: str,
    content: str,
    context_mode: str,
    roles: list[str] | None = None,
    stage: str | None = None,
    input_files: list[str] | None = None,
    acceptance: RunAcceptance | None = None,
) -> None:
    app_state = websocket.app.state
    socket_ref = weakref.ref(websocket)
    if acceptance is None:
        try:
            async with app_state.database.session() as session:
                acceptance = await accept_run(
                    session,
                    conversation_id=conversation_id,
                    client_request_id=client_request_id,
                    content=content,
                    requested_context_mode=context_mode,
                )
        except RunAcceptanceError as error:
            await send_command_error(
                websocket,
                code=error.code,
                message=error.message,
                client_request_id=client_request_id,
                conversation_id=conversation_id,
            )
            return
        except Exception:
            logger.exception("Failed to accept run request %s", client_request_id)
            await send_command_error(
                websocket,
                code="run_acceptance_failed",
                message="Не удалось создать запрос.",
                client_request_id=client_request_id,
                conversation_id=conversation_id,
            )
            return

    socket_available = True

    async def deliver_event(event: Event, **wire_fields: Any) -> None:
        nonlocal socket_available
        if not socket_available:
            return
        delivery_socket = socket_ref()
        if delivery_socket is None:
            socket_available = False
            return
        try:
            await send_stored_event(
                delivery_socket,
                event,
                acceptance.conversation_id,
                **wire_fields,
            )
        except (WebSocketDisconnect, RuntimeError):
            socket_available = False

    if acceptance.reset_event is not None:
        await deliver_event(acceptance.reset_event)
    await deliver_event(
        acceptance.accepted_event,
        idempotent_replay=acceptance.idempotent_replay,
    )
    del websocket

    if acceptance.idempotent_replay:
        return

    if app_state.settings.executor == "runner":
        await run_via_runner(app_state, acceptance, content, deliver_event, roles=roles, stage=stage, input_files=input_files)
        return

    process: subprocess.Popen[str] | None = None
    stderr_task: asyncio.Task[bool] | None = None
    stderr_session_unavailable = False
    assistant_parts: list[str] = []
    result_event: NormalizedEvent = normalized_event("result")
    has_result = False
    observed_session_id: str | None = None
    exit_code: int | None = None
    run_status = "failed"
    error_code: str | None = None
    tool_calls: dict[str, NormalizedEvent] = {}
    resume_session_id = (
        acceptance.resume_session_id
        if acceptance.context_mode == "resume"
        else None
    )
    terminal_error: BaseException | None = None
    terminal_event: Event | None = None
    workspace: Path | None = None

    async def publish_event(event_data: dict[str, Any]) -> Event:
        async with app_state.database.session() as session:
            stored_event = await append_event(session, acceptance.run_id, event_data)
        await deliver_event(stored_event)
        return stored_event

    async def persist_artifact(
        *,
        file_path: str,
        file_content: str,
    ) -> None:
        if workspace is None:
            raise ArtifactSaveError("Conversation workspace is unavailable")
        try:
            relative_path = normalize_artifact_path(workspace, file_path)
        except WorkspaceError:
            logger.warning(
                "Rejected artifact path outside workspace for run %s",
                acceptance.run_id,
            )
            return

        bounded_content, size_bytes, truncated = bounded_file_content(
            file_content,
            max_bytes=app_state.settings.artifact_max_download_bytes,
        )
        try:
            async with app_state.database.session() as session:
                stored_event = await append_artifact(
                    session,
                    acceptance.run_id,
                    path=relative_path,
                    content=bounded_content,
                    size_bytes=size_bytes,
                    truncated=truncated,
                    num_lines=file_content.count("\n") + 1,
                )
        except Exception as error:
            raise ArtifactSaveError("Artifact snapshot could not be saved") from error
        await deliver_event(stored_event)

    async def finalize_and_store_terminal() -> Event:
        assistant_content = "\n\n".join(assistant_parts).strip()
        async with app_state.database.session() as session:
            finalization = await finalize_run(
                session,
                acceptance.run_id,
                assistant_content=assistant_content or None,
                status=run_status,
                exit_code=exit_code,
                session_id=observed_session_id,
                duration_ms=result_event.get("duration_ms") if has_result else None,
                cost_usd=result_event.get("cost_usd") if has_result else None,
                turns=result_event.get("num_turns") if has_result else None,
                error_code=error_code,
            )
        return finalization.terminal_event

    try:
        workspace = conversation_workspace(
            app_state.workspace_root,
            acceptance.conversation_id,
            create=False,
        )
        if acceptance.context_mode == "resume" and not workspace.is_dir():
            raise WorkspaceError("Conversation workspace is unavailable")
        workspace = conversation_workspace(
            app_state.workspace_root,
            acceptance.conversation_id,
            create=True,
        )

        async with app_state.database.session() as session:
            started_event = await mark_run_running(session, acceptance.run_id)
        if started_event is not None:
            await deliver_event(started_event)

        process = await start_claude(
            content,
            str(workspace),
            resume_session_id,
            permission_mode=app_state.settings.claude_permission_mode,
        )
        stderr_task = asyncio.create_task(
            drain_stderr(process, run_id=acceptance.run_id)
        )

        async for line in process_output(process):
            for event_data in parse_event(line):
                event_type = event_data["type"]
                if event_type == "session":
                    session_id = event_data.get("session_id")
                    if session_id:
                        if resume_session_id and session_id != resume_session_id:
                            raise ClaudeSessionMismatchError(
                                "Claude returned a different session ID during resume"
                            )
                        observed_session_id = session_id
                    continue

                if event_type == "tool" and event_data.get("tool_id"):
                    tool_calls[str(event_data["tool_id"])] = event_data
                    continue

                if event_type == "tool_result":
                    tool_id = event_data.get("tool_id")
                    tool_call = tool_calls.pop(str(tool_id), None) if tool_id else None
                    if (
                        tool_call
                        and tool_call.get("name") == "Write"
                        and event_data.get("result_confirmed") is True
                        and event_data.get("is_error") is not True
                    ):
                        tool_input = tool_call.get("input")
                        if isinstance(tool_input, dict):
                            file_path = tool_input.get("file_path")
                            file_content = tool_input.get("content")
                            if isinstance(file_path, str) and isinstance(
                                file_content, str
                            ):
                                await persist_artifact(
                                    file_path=file_path,
                                    file_content=file_content,
                                )
                    continue

                if event_type == "message" and event_data.get("content"):
                    content_part = str(event_data["content"])
                    assistant_parts.append(content_part)
                    await publish_event(message_created_event(content_part))
                    continue

                if event_type == "result":
                    result_event = event_data
                    has_result = True
                    session_id = event_data.get("session_id")
                    if session_id:
                        if resume_session_id and session_id != resume_session_id:
                            raise ClaudeSessionMismatchError(
                                "Claude returned a different session ID during resume"
                            )
                        observed_session_id = session_id
                    continue

                if event_type in {"raw", "raw_event"}:
                    logger.warning(
                        "Ignored malformed Claude CLI output for run %s",
                        acceptance.run_id,
                    )

        exit_code = await wait_process(process)
        if stderr_task is not None:
            stderr_session_unavailable = await stderr_task
            stderr_task = None
        result_is_error = bool(result_event.get("is_error", False))
        if exit_code == 0 and not has_result:
            run_status = "outcome_unknown"
            error_code = "claude_session_state_uncertain"
        elif exit_code == 0 and not result_is_error:
            run_status = "completed"
            result_text = result_event.get("result")
            if not assistant_parts and isinstance(result_text, str) and result_text.strip():
                assistant_parts.append(result_text.strip())
                await publish_event(message_created_event(result_text.strip()))
        else:
            run_status = "failed"
            result_text = str(result_event.get("result") or "")
            error_code = (
                "claude_session_unavailable"
                if acceptance.context_mode == "resume"
                and stderr_session_unavailable
                else classify_session_unavailable(
                    acceptance,
                    exit_code=exit_code,
                    observed_session_id=observed_session_id,
                    result_text=result_text,
                )
            ) or "claude_cli_failed"
    except WorkspaceError:
        run_status = "failed"
        error_code = (
            "claude_session_unavailable"
            if acceptance.context_mode == "resume"
            else "claude_request_failed"
        )
    except ClaudeSessionMismatchError:
        run_status = "failed"
        error_code = "claude_session_mismatch"
    except ArtifactSaveError:
        logger.exception("Artifact save failed for run %s", acceptance.run_id)
        run_status = "failed"
        error_code = "artifact_save_failed"
    except asyncio.CancelledError as error:
        run_status = "outcome_unknown"
        error_code = "heartbeat_lost"
        terminal_error = error
    except Exception:
        logger.exception("Claude request failed for run %s", acceptance.run_id)
        run_status = "failed"
        error_code = "claude_request_failed"
    finally:
        await stop_process(process)
        if stderr_task is not None:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        terminal_event = await asyncio.shield(finalize_and_store_terminal())

    if terminal_error is not None:
        raise terminal_error

    await deliver_event(terminal_event)


async def handle_subscription(
    websocket: WebSocket,
    run_id: str,
    after_seq: int,
) -> None:
    page_size = 500
    terminal_statuses = TERMINAL_RUN_STATUSES
    cursor = after_seq
    settings = getattr(websocket.app.state, "settings", APP_SETTINGS)
    deadline = (
        asyncio.get_running_loop().time()
        + settings.subscription_window_seconds
    )

    while True:
        async with websocket.app.state.database.session() as session:
            page = await list_run_events(
                session,
                run_id,
                after_seq=cursor,
                limit=page_size,
            )

        if page is None:
            await send_websocket_payload(
                websocket,
                {
                    "type": "command.error",
                    "request_type": "run.subscribe",
                    "code": "run_not_found",
                    "message": "Run not found",
                    "run_id": run_id,
                },
            )
            return

        for event in page.events:
            await send_stored_event(websocket, event, page.conversation_id)

        if page.events:
            cursor = page.events[-1].seq

        if (
            len(page.events) < page_size
            and page.run_status in terminal_statuses
        ):
            await send_websocket_payload(
                websocket,
                {
                    "type": "replay.complete",
                    "run_id": run_id,
                    "after_seq": after_seq,
                    "last_seq": cursor,
                    "status": page.run_status,
                    "terminal": True,
                },
            )
            return

        if asyncio.get_running_loop().time() >= deadline:
            await send_websocket_payload(
                websocket,
                {
                    "type": "replay.complete",
                    "run_id": run_id,
                    "after_seq": after_seq,
                    "last_seq": cursor,
                    "status": page.run_status,
                    "terminal": False,
                },
            )
            return

        if len(page.events) == page_size:
            continue

        await asyncio.sleep(settings.subscription_poll_interval_seconds)


def valid_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _log_background_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception("Background gateway task failed")


async def start_run_task(
    websocket: WebSocket,
    coroutine: Any,
    *,
    allow_overflow: bool = False,
) -> bool:
    state = websocket.app.state
    async with state.run_slot_lock:
        if (
            state.run_task_count >= state.settings.max_concurrent_runs
            and not allow_overflow
        ):
            close = getattr(coroutine, "close", None)
            if close is not None:
                close()
            return False
        state.run_task_count += 1

    async def run_with_slot() -> None:
        try:
            await coroutine
        finally:
            async with state.run_slot_lock:
                state.run_task_count -= 1

    task = asyncio.create_task(run_with_slot())
    run_tasks: set[asyncio.Task[Any]] = state.run_tasks
    run_tasks.add(task)
    task.add_done_callback(run_tasks.discard)
    task.add_done_callback(_log_background_task)
    return True


def start_subscription_task(
    websocket: WebSocket,
    run_id: str,
    after_seq: int,
) -> None:
    subscriptions = getattr(websocket, "_gateway_subscriptions", None)
    if subscriptions is None:
        subscriptions = {}
        setattr(websocket, "_gateway_subscriptions", subscriptions)

    existing = subscriptions.pop(run_id, None)
    if existing is not None:
        existing.cancel()

    task = asyncio.create_task(handle_subscription(websocket, run_id, after_seq))
    subscriptions[run_id] = task

    def remove(completed: asyncio.Task[Any]) -> None:
        if subscriptions.get(run_id) is completed:
            subscriptions.pop(run_id, None)
        _log_background_task(completed)

    task.add_done_callback(remove)


async def dispatch_websocket_message(websocket: WebSocket, raw: str) -> None:
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        content = raw.strip()
        if not content:
            await send_command_error(
                websocket,
                code="invalid_command",
                message="Пустой запрос не допускается.",
            )
            return
        started = await start_run_task(
            websocket,
            handle_run_create(
                websocket,
                conversation_id=None,
                client_request_id=str(uuid4()),
                content=content,
                context_mode="resume",
            ),
        )
        if not started:
            await send_command_error(
                websocket,
                code="gateway_busy",
                message="Достигнут лимит одновременных запусков.",
                retryable=True,
            )
        return

    if not isinstance(command, dict):
        await send_command_error(
            websocket,
            code="invalid_command",
            message="Команда должна быть JSON-объектом.",
        )
        return

    if command.get("type") == "run.create":
        conversation_id = command.get("conversation_id")
        client_request_id = command.get("client_request_id")
        content = command.get("content")
        context_mode = command.get("context_mode", "resume")
        if (
            (conversation_id is not None and not valid_uuid(conversation_id))
            or not valid_uuid(client_request_id)
            or not isinstance(content, str)
            or not content.strip()
            or context_mode not in {"resume", "reset"}
        ):
            await send_command_error(
                websocket,
                code="invalid_command",
                message="Некорректная команда run.create.",
                client_request_id=(
                    client_request_id if isinstance(client_request_id, str) else None
                ),
                conversation_id=(
                    conversation_id if isinstance(conversation_id, str) else None
                ),
            )
            return

        state = websocket.app.state
        async with state.run_slot_lock:
            at_capacity = (
                state.run_task_count >= state.settings.max_concurrent_runs
            )
        allow_overflow = False
        if at_capacity:
            async with state.database.session() as session:
                allow_overflow = await client_request_exists(
                    session,
                    client_request_id,
                )

        started = await start_run_task(
            websocket,
            handle_run_create(
                websocket,
                conversation_id=conversation_id,
                client_request_id=client_request_id,
                content=content.strip(),
                context_mode=context_mode,
            ),
            allow_overflow=allow_overflow,
        )
        if not started:
            await send_command_error(
                websocket,
                code="gateway_busy",
                message="Достигнут лимит одновременных запусков.",
                client_request_id=client_request_id,
                conversation_id=conversation_id,
                retryable=True,
            )
        return

    if command.get("type") == "run.subscribe":
        run_id = command.get("run_id")
        after_seq = command.get("after_seq", 0)
        if (
            not valid_uuid(run_id)
            or not isinstance(after_seq, int)
            or isinstance(after_seq, bool)
            or after_seq < 0
        ):
            await send_websocket_payload(
                websocket,
                {
                    "type": "command.error",
                    "request_type": "run.subscribe",
                    "code": "invalid_subscription",
                    "message": "Некорректная команда run.subscribe.",
                },
            )
            return

        start_subscription_task(websocket, run_id, after_seq)
        return

    await send_command_error(
        websocket,
        code="invalid_command",
        message="Неизвестный тип команды.",
    )


def create_app(
    database: Database | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    selected_settings = settings or APP_SETTINGS
    selected_database = database or Database(selected_settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.workspace_root = validate_workspace_root(
            app.state.settings.workspace_root,
            project_root=PROJECT_ROOT,
            database_url=app.state.database.url,
        )
        lock = acquire_gateway_lock(
            gateway_lock_path(
                app.state.database.url,
                app.state.workspace_root,
            )
        )
        database_ready = False
        supervisor: asyncio.Task[None] | None = None
        runner_manager: ContainerManager | None = getattr(app.state, "runner_manager", None)
        try:
            await app.state.database.migrate()
            database_ready = True
            async with app.state.database.session() as session:
                await reconcile_orphaned_runs(session)
            if runner_manager is not None:
                await runner_manager.restore()
                await runner_manager.bridge.start()
                supervisor = asyncio.create_task(runner_manager.supervise())
            yield
        finally:
            if supervisor is not None:
                supervisor.cancel()
                await asyncio.gather(supervisor, return_exceptions=True)
            if runner_manager is not None:
                await runner_manager.bridge.stop()
            try:
                tasks = list(app.state.run_tasks)
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if database_ready:
                    async with app.state.database.session() as session:
                        await reconcile_orphaned_runs(session)
                    await app.state.database.dispose()
            finally:
                release_gateway_lock(lock)

    application = FastAPI(lifespan=lifespan)
    application.state.database = selected_database
    application.state.settings = selected_settings
    application.state.run_tasks = set()
    application.state.run_slot_lock = asyncio.Lock()
    application.state.run_task_count = 0
    application.include_router(history_router)
    application.include_router(projects_adapter_router)

    if selected_settings.executor == "runner":
        selected_settings.runner_transport_db.parent.mkdir(parents=True, exist_ok=True)
        transport = RunnerTransport(selected_settings.runner_transport_db)
        bridge = RunnerBridge(
            transport,
            heartbeat_timeout=selected_settings.runner_heartbeat_timeout_seconds,
        )
        application.state.runner_transport = transport
        application.state.runner_manager = ContainerManager(selected_settings, bridge, transport)
        # Runner подключается сам: входящий порт у контейнера проекта не нужен.
        application.include_router(create_runner_router(bridge))

        @application.get("/api/conversations/{conversation_id}/runner")
        async def runner_status(conversation_id: str) -> dict[str, Any]:
            manager: ContainerManager = application.state.runner_manager
            try:
                project = manager.project_id(conversation_id)
            except ContainerError:
                raise HTTPException(status_code=404, detail="Runner not found") from None
            return await asyncio.to_thread(manager.describe, project)

    @application.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()

        try:
            while True:
                raw = await websocket.receive_text()
                await dispatch_websocket_message(websocket, raw)
        except WebSocketDisconnect:
            pass
        finally:
            subscriptions = getattr(websocket, "_gateway_subscriptions", {})
            for task in subscriptions.values():
                task.cancel()
            if subscriptions:
                await asyncio.gather(
                    *subscriptions.values(),
                    return_exceptions=True,
                )
            try:
                await websocket.close()
            except Exception:
                pass

    return application


app = create_app()
