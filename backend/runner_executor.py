"""Исполнение запроса диалога в runner-контейнере (Settings.executor == "runner").

Запрос ставится командой в транспорт, события events.v0 из транспорта переносятся
в историю backend (те же события, что видит браузер в режиме cli). Этот модуль —
доверенный адаптер хранения: читает байты артефакта из контейнера, сверяет sha256
и размер, сохраняет снимок и только после этого вызывает confirm_saved.

Вопрос агента (facilitator.question) завершает текущий запрос текстом вопроса;
следующее сообщение пользователя в диалоге уходит runner ответом на него.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from backend.containers import ContainerError, ContainerManager
from backend.models import Event
from backend.parser import bounded_file_content
from backend.materials import MATERIALS_DIR, conversation_materials, materials_note
from backend.public_events import message_created_event, question_asked_event
from backend.repository import (
    RunAcceptance,
    append_artifact,
    append_event,
    finalize_run,
    mark_run_running,
)
from backend.runner_transport import RunnerTransport, TransportError


logger = logging.getLogger(__name__)
POLL_SECONDS = 0.25


class RunnerRunError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def session_marker(project: str) -> str:
    """Постоянный идентификатор контекста: сессию SDK держит сам runner на томе проекта."""
    return f"runner-{project}"


def question_text(payload: dict[str, Any]) -> str:
    lines = ["Агенту нужен ответ, чтобы продолжить:"]
    for question in payload["questions"]:
        header = f"{question['header']}: " if question.get("header") else ""
        lines.append(f"\n{header}{question['text']}")
        for option in question["options"]:
            description = f" — {option['description']}" if option.get("description") else ""
            lines.append(f"- {option['label']}{description}")
    lines.append("\nОтветьте следующим сообщением.")
    return "\n".join(lines)


async def run_via_runner(
    app_state: Any,
    acceptance: RunAcceptance,
    content: str,
    deliver_event: Callable[[Event], Awaitable[None]],
) -> None:
    manager: ContainerManager = app_state.runner_manager
    transport: RunnerTransport = app_state.runner_transport
    database = app_state.database
    run_id = acceptance.run_id
    project = manager.project_id(acceptance.conversation_id)

    parts: list[str] = []
    usage: dict[str, Any] = {}
    run_status, error_code = "failed", None
    transport_run: str | None = None
    keep_transport_run = False
    terminal_error: BaseException | None = None

    async def publish(event_data: dict[str, Any]) -> Event:
        async with database.session() as session:
            stored = await append_event(session, run_id, event_data)
        await deliver_event(stored)
        return stored

    async def save_artifact(payload: dict[str, Any]) -> None:
        data = await manager.read_file(project, payload["path"])
        if "sha256" in payload and hashlib.sha256(data).hexdigest() != payload["sha256"]:
            raise RunnerRunError("artifact_hash_mismatch")
        if "size_bytes" in payload and len(data) != payload["size_bytes"]:
            raise RunnerRunError("artifact_size_mismatch")
        text = data.decode("utf-8", errors="replace")
        bounded, size_bytes, truncated = bounded_file_content(
            text, max_bytes=app_state.settings.artifact_max_download_bytes,
        )
        async with database.session() as session:
            stored = await append_artifact(
                session, run_id, path=payload["path"], content=bounded,
                size_bytes=size_bytes, truncated=truncated, num_lines=text.count("\n") + 1,
            )
        await deliver_event(stored)
        transport.confirm_saved(project, transport_run, payload["artifact_id"], {
            "artifact_id": payload["artifact_id"],
            "version_id": payload["commit_sha"],
            "size_bytes": len(data),
            "download": f"/api/runs/{run_id}/events/{stored.seq}/file",
        })

    async def finalize() -> Event:
        async with database.session() as session:
            finalization = await finalize_run(
                session, run_id,
                assistant_content="\n\n".join(parts).strip() or None,
                status=run_status,
                exit_code=None,
                session_id=session_marker(project) if run_status == "completed" else None,
                duration_ms=usage.get("duration_ms"),
                cost_usd=usage.get("cost_usd"),
                turns=usage.get("num_turns"),
                error_code=error_code,
            )
        return finalization.terminal_event

    try:
        if (acceptance.context_mode == "resume"
                and acceptance.resume_session_id != session_marker(project)):
            # Диалог начат в режиме cli: его контекст — сессия Claude CLI, которой у runner нет.
            raise RunnerRunError("claude_session_unavailable")
        if acceptance.context_mode == "reset":
            await manager.discard(project, reason="context_reset")

        async with database.session() as session:
            started = await mark_run_running(session, run_id)
        if started is not None:
            await deliver_event(started)

        await manager.ensure_running(project, run_id=run_id)
        materials = await conversation_materials(database, acceptance.conversation_id)
        if materials is not None and materials.files:
            await manager.write_files(project, MATERIALS_DIR, materials.files)
        active = transport.active_run(project)
        if active is not None and active["status"] == "waiting_for_input" and active["question"]:
            transport_run = active["run_id"]
            after_seq = transport.events(project, transport_run)[-1]["seq"]
            question = active["question"]
            transport.answer(project, transport_run, run_id, question["question_id"],
                             {item["text"]: content for item in question["questions"]})
        else:
            if active is not None:
                if active["run_id"] in manager.relaying:
                    raise RunnerRunError("runner_busy")
                # Хозяина у запуска нет (backend перезапускался): закрыть, иначе проект заблокирован.
                transport.abandon(project, active["run_id"], "gateway_restarted")
            transport_run, after_seq = run_id, 0
            transport.message(project, transport_run, run_id, content + materials_note(materials))

        manager.relaying.add(transport_run)
        done = False
        while not done:
            for event in transport.events(project, transport_run, after_seq):
                after_seq = event["seq"]
                manager.touch(project)
                kind, payload = event["type"], event["payload"]
                if kind == "message.created" and payload["text"].strip():
                    parts.append(payload["text"].strip())
                    await publish(message_created_event(payload["text"].strip()))
                elif kind == "artifact.created":
                    await save_artifact(payload)
                elif kind == "usage.reported":
                    usage = payload
                elif kind == "facilitator.question":
                    text = question_text(payload)
                    parts.append(text)
                    await publish(message_created_event(text))
                    # Для интерфейса проекта: варианты ответа кнопками, а не только текстом.
                    await publish(question_asked_event(payload["questions"]))
                    run_status, keep_transport_run, done = "completed", True, True
                elif kind == "run.completed":
                    run_status, done = "completed", True
                elif kind == "run.failed":
                    run_status, error_code, done = "failed", f"runner_{payload['code']}", True
                elif kind == "run.outcome_unknown":
                    run_status, error_code, done = "outcome_unknown", payload["code"], True
                if done:
                    break
            if not done:
                await asyncio.sleep(POLL_SECONDS)
    except (RunnerRunError, ContainerError) as error:
        run_status, error_code = "failed", str(error)
    except TransportError as error:
        logger.warning("Runner transport rejected run %s: %s", run_id, error)
        run_status, error_code = "failed", f"runner_{error}"
    except asyncio.CancelledError as error:
        run_status, error_code = "outcome_unknown", "heartbeat_lost"
        terminal_error = error
    except Exception:
        logger.exception("Runner request failed for run %s", run_id)
        run_status, error_code = "failed", "claude_request_failed"
    finally:
        if transport_run is not None:
            manager.relaying.discard(transport_run)
            if run_status != "completed" and not keep_transport_run and terminal_error is None:
                try:
                    transport.abandon(project, transport_run, error_code or "gateway_failed")
                except TransportError:
                    pass
        terminal_event = await asyncio.shield(finalize())

    if terminal_error is not None:
        raise terminal_error
    await deliver_event(terminal_event)
