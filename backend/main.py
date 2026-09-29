import asyncio
import json
import logging
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from backend import models  # noqa: F401
from backend.api.history import router as history_router
from backend.config import Settings
from backend.db import Database
from backend.models import Event
from backend.parser import NormalizedEvent, normalized_event, parse_event
from backend.repository import (
    RunAcceptance,
    RunAcceptanceError,
    append_event,
    accept_run,
    finalize_run,
    list_run_events,
    mark_run_running,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_SETTINGS = Settings()
logger = logging.getLogger(__name__)


class ClaudeSessionMismatchError(RuntimeError):
    pass


def write_prompt(process: subprocess.Popen[str], prompt: str) -> None:
    if process.stdin is None:
        process.terminate()
        raise RuntimeError("Claude CLI stdin is unavailable")

    process.stdin.write(prompt)
    process.stdin.close()


async def start_claude(
    prompt: str,
    resume_session_id: str | None = None,
) -> subprocess.Popen[str]:
    arguments = [
        "claude",
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--permission-mode",
        APP_SETTINGS.claude_permission_mode,
    ]
    if resume_session_id:
        arguments.extend(["--resume", resume_session_id])

    process = await asyncio.to_thread(
        subprocess.Popen,
        arguments,
        cwd=PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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
    return {
        **event.payload,
        "type": event.type,
        "conversation_id": conversation_id,
        "run_id": event.run_id,
        "seq": event.seq,
    }


async def send_stored_event(
    websocket: WebSocket,
    event: Event,
    conversation_id: str,
    **wire_fields: Any,
) -> None:
    payload = websocket_event(event, conversation_id)
    payload.update(wire_fields)
    await websocket.send_text(json.dumps(payload, ensure_ascii=False))


async def send_command_error(
    websocket: WebSocket,
    *,
    code: str,
    message: str,
    client_request_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    await websocket.send_text(
        json.dumps(
            {
                "type": "command.error",
                "request_type": "run.create",
                "client_request_id": client_request_id,
                "conversation_id": conversation_id,
                "code": code,
                "message": message,
                "retryable": False,
            },
            ensure_ascii=False,
        )
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
    unavailable_markers = (
        "no conversation found",
        "unable to resume",
        "session not found",
    )
    if any(marker in normalized for marker in unavailable_markers):
        return "claude_session_unavailable"
    return None


async def handle_run_create(
    websocket: WebSocket,
    *,
    conversation_id: str | None,
    client_request_id: str,
    content: str,
    context_mode: str,
) -> None:
    try:
        async with websocket.app.state.database.session() as session:
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
        try:
            await send_stored_event(
                websocket,
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

    if acceptance.idempotent_replay:
        return

    process: subprocess.Popen[str] | None = None
    assistant_parts: list[str] = []
    result_event: NormalizedEvent = normalized_event("result")
    has_result = False
    observed_session_id: str | None = None
    exit_code: int | None = None
    run_status = "failed"
    run_error: str | None = None
    error_code: str | None = None
    raw_error_parts: list[str] = []
    resume_session_id = (
        acceptance.resume_session_id
        if acceptance.context_mode == "resume"
        else None
    )
    terminal_error: BaseException | None = None
    terminal_event: Event | None = None

    async def publish_event(event_data: NormalizedEvent) -> Event:
        async with websocket.app.state.database.session() as session:
            stored_event = await append_event(session, acceptance.run_id, event_data)
        await deliver_event(stored_event)
        return stored_event

    async def finalize_and_store_terminal() -> Event:
        assistant_content = "\n\n".join(assistant_parts).strip()
        if not assistant_content and has_result and result_event.get("result"):
            assistant_content = str(result_event["result"]).strip()

        async with websocket.app.state.database.session() as session:
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
                error=run_error,
                error_code=error_code,
                result_event=result_event if has_result else None,
            )
        return finalization.terminal_event

    try:
        async with websocket.app.state.database.session() as session:
            await mark_run_running(session, acceptance.run_id)

        process = await start_claude(content, resume_session_id)

        async for line in process_output(process):
            for event_data in parse_event(line):
                event_type = event_data["type"]
                if event_type == "session":
                    session_id = event_data.get("session_id")
                    if session_id:
                        if (
                            resume_session_id
                            and session_id != resume_session_id
                        ):
                            raise ClaudeSessionMismatchError(
                                "Claude вернул другой session ID при resume."
                            )
                        observed_session_id = session_id
                elif event_type == "message" and event_data.get("content"):
                    assistant_parts.append(str(event_data["content"]))
                elif event_type == "result":
                    result_event = event_data
                    has_result = True
                    session_id = event_data.get("session_id")
                    if session_id:
                        if (
                            resume_session_id
                            and session_id != resume_session_id
                        ):
                            raise ClaudeSessionMismatchError(
                                "Claude вернул другой session ID при resume."
                            )
                        observed_session_id = session_id
                    continue
                elif event_type == "raw" and event_data.get("content"):
                    raw_error_parts.append(str(event_data["content"]))

                await publish_event(event_data)

        exit_code = await wait_process(process)
        result_is_error = bool(result_event.get("is_error", False))
        if exit_code == 0 and not result_is_error:
            run_status = "completed"
        else:
            run_status = "failed"
            result_text = str(result_event.get("result") or "\n".join(raw_error_parts))
            error_code = classify_session_unavailable(
                acceptance,
                exit_code=exit_code,
                observed_session_id=observed_session_id,
                result_text=result_text,
            ) or "claude_cli_failed"
            run_error = result_text or f"Claude CLI exited with code {exit_code}"
    except ClaudeSessionMismatchError as error:
        run_status = "failed"
        error_code = "claude_session_mismatch"
        run_error = str(error)
    except asyncio.CancelledError as error:
        run_status = "cancelled"
        error_code = "backend_task_cancelled"
        run_error = "Backend task cancelled"
        terminal_error = error
    except Exception as error:
        logger.exception("Claude request failed for run %s", acceptance.run_id)
        run_status = "failed"
        error_code = "claude_request_failed"
        run_error = f"Claude request failed: {str(error) or type(error).__name__}"
    finally:
        await stop_process(process)
        terminal_event = await finalize_and_store_terminal()

    if terminal_error is not None:
        raise terminal_error

    await deliver_event(terminal_event)


async def handle_subscription(
    websocket: WebSocket,
    run_id: str,
    after_seq: int,
) -> None:
    page_size = 500
    terminal_statuses = {"completed", "failed", "cancelled"}
    cursor = after_seq

    while True:
        async with websocket.app.state.database.session() as session:
            page = await list_run_events(
                session,
                run_id,
                after_seq=cursor,
                limit=page_size,
            )

        if page is None:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "command.error",
                        "request_type": "run.subscribe",
                        "code": "run_not_found",
                        "message": "Run not found",
                        "run_id": run_id,
                    },
                    ensure_ascii=False,
                )
            )
            return

        for event in page.events:
            await send_stored_event(websocket, event, page.conversation_id)

        if page.events:
            cursor = page.events[-1].seq

        if len(page.events) < page_size:
            if page.run_status in terminal_statuses:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "replay.complete",
                            "run_id": run_id,
                            "after_seq": after_seq,
                            "last_seq": cursor,
                            "status": page.run_status,
                        },
                        ensure_ascii=False,
                    )
                )
                return

            await asyncio.sleep(0.25)


def valid_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


async def dispatch_websocket_message(websocket: WebSocket, raw: str) -> None:
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        await handle_run_create(
            websocket,
            conversation_id=None,
            client_request_id=str(uuid4()),
            content=raw.strip(),
            context_mode="resume",
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

        await handle_run_create(
            websocket,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            content=content.strip(),
            context_mode=context_mode,
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
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "command.error",
                        "request_type": "run.subscribe",
                        "code": "invalid_subscription",
                        "message": "Некорректная команда run.subscribe.",
                    },
                    ensure_ascii=False,
                )
            )
            return

        await handle_subscription(websocket, run_id, after_seq)
        return

    await send_command_error(
        websocket,
        code="invalid_command",
        message="Неизвестный тип команды.",
    )


def create_app(database: Database | None = None) -> FastAPI:
    selected_database = database or Database(APP_SETTINGS.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.database.migrate()
        try:
            yield
        finally:
            await app.state.database.dispose()

    application = FastAPI(lifespan=lifespan)
    application.state.database = selected_database
    application.include_router(history_router)

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
            try:
                await websocket.close()
            except Exception:
                pass

    return application


app = create_app()
