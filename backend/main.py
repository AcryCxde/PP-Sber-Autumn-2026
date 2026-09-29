import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from backend import models  # noqa: F401
from backend.api.history import router as history_router
from backend.config import Settings
from backend.db import Database
from backend.parser import parse_event
from backend.repository import create_request, finalize_run, mark_run_running


PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def start_claude(prompt: str) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(
        "claude",
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        cwd=PROJECT_ROOT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    if process.stdin is None:
        process.terminate()
        raise RuntimeError("Claude CLI stdin is unavailable")

    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    await process.stdin.wait_closed()

    return process


async def process_output(
    process: asyncio.subprocess.Process,
) -> AsyncIterator[str]:
    if process.stdout is None:
        raise RuntimeError("Claude CLI stdout is unavailable")

    while line := await process.stdout.readline():
        yield line.decode("utf-8", errors="replace")


async def stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


async def handle_prompt(websocket: WebSocket, prompt: str) -> None:
    process: asyncio.subprocess.Process | None = None
    run_id: str | None = None
    assistant_parts: list[str] = []
    result_event: dict[str, Any] = {}
    claude_session_id: str | None = None
    exit_code: int | None = None
    run_status = "failed"
    run_error: str | None = None
    finalized = False
    terminal_error: BaseException | None = None
    send_error = False

    async def persist_run() -> None:
        nonlocal finalized

        if run_id is None or finalized:
            return

        assistant_content = "\n\n".join(assistant_parts).strip()
        if not assistant_content and result_event.get("result"):
            assistant_content = str(result_event["result"]).strip()

        async with websocket.app.state.database.session() as session:
            await finalize_run(
                session,
                run_id,
                assistant_content=assistant_content or None,
                status=run_status,
                exit_code=exit_code,
                session_id=claude_session_id,
                duration_ms=result_event.get("duration_ms"),
                cost_usd=result_event.get("cost_usd"),
                turns=result_event.get("num_turns"),
                error=run_error,
            )
        finalized = True

    try:
        async with websocket.app.state.database.session() as session:
            created = await create_request(session, prompt)
            run_id = created.run_id

        await websocket.send_text(
            json.dumps(
                {
                    "type": "history",
                    "conversation_id": created.conversation_id,
                    "run_id": created.run_id,
                    "message_id": created.message_id,
                },
                ensure_ascii=False,
            )
        )

        async with websocket.app.state.database.session() as session:
            await mark_run_running(session, created.run_id)

        process = await start_claude(prompt)

        async for line in process_output(process):
            event = parse_event(line)
            if event is None:
                continue

            event_type = event.get("type")
            if event_type == "session":
                claude_session_id = event.get("session_id")
            elif event_type == "message" and event.get("content"):
                assistant_parts.append(event["content"])
            elif event_type == "result":
                result_event = event
                claude_session_id = event.get("session_id") or claude_session_id
                continue

            await websocket.send_text(json.dumps(event, ensure_ascii=False))

        exit_code = await process.wait()
        if exit_code == 0 and not result_event.get("is_error", False):
            run_status = "completed"
        else:
            run_status = "failed"
            run_error = str(
                result_event.get("result")
                or f"Claude CLI exited with code {exit_code}"
            )

        await persist_run()
        if result_event:
            await websocket.send_text(json.dumps(result_event, ensure_ascii=False))
    except WebSocketDisconnect as error:
        run_status = "cancelled"
        run_error = "Client disconnected"
        terminal_error = error
    except asyncio.CancelledError as error:
        run_status = "cancelled"
        run_error = "Backend task cancelled"
        terminal_error = error
    except Exception:
        run_status = "failed"
        run_error = "Claude request failed"
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
        await websocket.send_text(
            json.dumps(
                {
                    "type": "raw",
                    "content": run_error,
                },
                ensure_ascii=False,
            )
        )


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
                prompt = await websocket.receive_text()
                await handle_prompt(websocket, prompt)
        except WebSocketDisconnect:
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    return application


app = create_app()
