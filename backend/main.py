import asyncio
import json
import logging
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from backend import models  # noqa: F401
from backend.api.history import router as history_router
from backend.config import Settings
from backend.db import Database
from backend.models import Event
from backend.parser import NormalizedEvent, normalized_event, parse_event
from backend.repository import (
    append_event,
    create_request,
    finalize_run,
    list_run_events,
    mark_run_running,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


def write_prompt(process: subprocess.Popen[str], prompt: str) -> None:
    if process.stdin is None:
        process.terminate()
        raise RuntimeError("Claude CLI stdin is unavailable")

    process.stdin.write(prompt)
    process.stdin.close()


async def start_claude(prompt: str) -> subprocess.Popen[str]:
    process = await asyncio.to_thread(
        subprocess.Popen,
        [
            "claude",
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
        ],
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
) -> None:
    await websocket.send_text(
        json.dumps(
            websocket_event(event, conversation_id),
            ensure_ascii=False,
        )
    )


async def handle_prompt(websocket: WebSocket, prompt: str) -> None:
    process: subprocess.Popen[str] | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    assistant_parts: list[str] = []
    result_event: NormalizedEvent = normalized_event("result")
    has_result = False
    pending_results: list[Event] = []
    claude_session_id: str | None = None
    exit_code: int | None = None
    run_status = "failed"
    run_error: str | None = None
    finalized = False
    terminal_error: BaseException | None = None
    send_error = False

    async def store_event(event_data: NormalizedEvent) -> Event:
        if run_id is None:
            raise RuntimeError("Cannot store an event before creating a run")

        async with websocket.app.state.database.session() as session:
            return await append_event(session, run_id, event_data)

    async def publish_event(event_data: NormalizedEvent) -> Event:
        if conversation_id is None:
            raise RuntimeError("Cannot publish an event without a conversation")

        stored_event = await store_event(event_data)
        await send_stored_event(websocket, stored_event, conversation_id)
        return stored_event

    async def persist_run() -> None:
        nonlocal finalized

        if run_id is None or finalized:
            return

        assistant_content = "\n\n".join(assistant_parts).strip()
        if not assistant_content and has_result and result_event.get("result"):
            assistant_content = str(result_event["result"]).strip()

        async with websocket.app.state.database.session() as session:
            await finalize_run(
                session,
                run_id,
                assistant_content=assistant_content or None,
                status=run_status,
                exit_code=exit_code,
                session_id=claude_session_id,
                duration_ms=result_event.get("duration_ms") if has_result else None,
                cost_usd=result_event.get("cost_usd") if has_result else None,
                turns=result_event.get("num_turns") if has_result else None,
                error=run_error,
            )
        finalized = True

    try:
        async with websocket.app.state.database.session() as session:
            created = await create_request(session, prompt)
            conversation_id = created.conversation_id
            run_id = created.run_id

        await publish_event(
            normalized_event(
                "history",
                message_id=created.message_id,
            )
        )

        async with websocket.app.state.database.session() as session:
            await mark_run_running(session, created.run_id)

        process = await start_claude(prompt)

        async for line in process_output(process):
            for event_data in parse_event(line):
                event_type = event_data["type"]
                if event_type == "session":
                    claude_session_id = event_data.get("session_id")
                elif event_type == "message" and event_data.get("content"):
                    assistant_parts.append(str(event_data["content"]))
                elif event_type == "result":
                    result_event = event_data
                    has_result = True
                    claude_session_id = (
                        event_data.get("session_id") or claude_session_id
                    )
                    pending_results.append(await store_event(event_data))
                    continue

                await publish_event(event_data)

        exit_code = await wait_process(process)
        if exit_code == 0 and not result_event.get("is_error", False):
            run_status = "completed"
        else:
            run_status = "failed"
            run_error = str(
                result_event.get("result")
                or f"Claude CLI exited with code {exit_code}"
            )

        await persist_run()
        if conversation_id is not None:
            for stored_result in pending_results:
                await send_stored_event(
                    websocket,
                    stored_result,
                    conversation_id,
                )
    except WebSocketDisconnect as error:
        run_status = "cancelled"
        run_error = "Client disconnected"
        terminal_error = error
    except asyncio.CancelledError as error:
        run_status = "cancelled"
        run_error = "Backend task cancelled"
        terminal_error = error
    except Exception as error:
        logger.exception("Claude request failed for run %s", run_id)
        run_status = "failed"
        error_details = str(error) or type(error).__name__
        run_error = f"Claude request failed: {error_details}"
        send_error = True
    finally:
        await stop_process(process)
        try:
            await persist_run()
        except Exception:
            if terminal_error is None:
                raise

    if terminal_error is not None:
        raise terminal_error

    if send_error:
        error_event = normalized_event(
            "raw",
            content=run_error,
            error_code="claude_request_failed",
        )
        try:
            await publish_event(error_event)
        except Exception:
            await websocket.send_text(
                json.dumps(error_event, ensure_ascii=False)
            )


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
                        "type": "raw",
                        "content": "Run not found",
                        "error_code": "run_not_found",
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


def parse_websocket_command(raw: str) -> tuple[str, dict[str, Any] | str]:
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        return "prompt", raw

    if not isinstance(command, dict):
        return "prompt", raw

    command_type = command.get("type")
    if command_type == "run.subscribe":
        return "subscribe", command
    if command_type == "run.create" and isinstance(command.get("content"), str):
        return "prompt", command["content"]

    return "prompt", raw


async def dispatch_websocket_message(websocket: WebSocket, raw: str) -> None:
    command_type, payload = parse_websocket_command(raw)
    if command_type == "prompt":
        await handle_prompt(websocket, str(payload))
        return

    command = payload
    if not isinstance(command, dict):
        raise TypeError("Subscribe command must be an object")

    run_id = command.get("run_id")
    after_seq = command.get("after_seq", 0)
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(after_seq, int)
        or isinstance(after_seq, bool)
        or after_seq < 0
    ):
        await websocket.send_text(
            json.dumps(
                {
                    "type": "raw",
                    "content": "Invalid run.subscribe command",
                    "error_code": "invalid_subscription",
                },
                ensure_ascii=False,
            )
        )
        return

    await handle_subscription(websocket, run_id, after_seq)


def create_app(database: Database | None = None) -> FastAPI:
    selected_database = database or Database(Settings().database_url)

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
