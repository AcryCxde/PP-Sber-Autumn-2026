"""Клиент gateway: runner сам подключается по WebSocket (`docs/2.4/protocol.md`).

Исходящий поток — хвост журнала `events.jsonl`, переведённый адаптером v0; входящий — команды,
которые проверяются и кладутся в файловый inbox. Так `Session` не знает о сети, а команда и событие
переживают обрыв и рестарт по тем же правилам, что и без gateway.
"""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, assert_never, final

from ctrunner.diag import diag
from ctrunner.inbox import put
from ctrunner.protocol import (
    Command,
    ForkAnswerCmd,
    JsonValue,
    MessageCmd,
    ProtocolError,
    StopCmd,
    parse_command,
)
from ctrunner.v0 import Draft, Translator, parse_row

WIRE_VERSION: Final = 1
FRAME_ENVELOPE: Final = ("v", "type")


def hello_message(
    *, project_id: str, runner_version: str, session_id: str | None, last_seq: int
) -> dict[str, JsonValue]:
    return {
        "v": WIRE_VERSION, "type": "hello", "project_id": project_id,
        "runner_version": runner_version, "session_id": session_id, "last_seq": last_seq,
    }  # fmt: skip


def parse_welcome(raw: object) -> int:
    if not isinstance(raw, dict) or raw.get("type") != "welcome":
        raise ProtocolError("expected welcome")
    acked = raw.get("acked_seq")
    if isinstance(acked, bool) or not isinstance(acked, int) or acked < 0:
        raise ProtocolError("welcome.acked_seq must be a non-negative integer")
    return acked


def event_message(journal_seq: int, draft: Draft) -> dict[str, JsonValue]:
    """Вложенный `event`: `type` конверта и `type` события v0 иначе совпали бы по имени."""
    body: dict[str, JsonValue] = {
        "event_id": draft.event_id,
        "project_id": draft.project_id,
        "run_id": draft.run_id,
        "occurred_at": draft.occurred_at.isoformat().replace("+00:00", "Z"),
        "type": draft.type.value,
        "payload": draft.payload,
    }
    return {"v": WIRE_VERSION, "type": "event", "journal_seq": journal_seq, "event": body}


def heartbeat_message(health: Mapping[str, object]) -> dict[str, JsonValue] | None:
    phase, state = health.get("phase"), health.get("health")
    progress = health.get("last_progress_at")
    if not isinstance(phase, str) or not isinstance(state, str):
        return None
    if isinstance(progress, bool) or not isinstance(progress, int | float):
        return None
    return {"v": WIRE_VERSION, "type": "heartbeat", "phase": phase, "health": state,
            "last_progress_at": float(progress), "turn_id": None}  # fmt: skip


def command_ack(
    command_id: str | None, status: str, reason: str | None = None
) -> dict[str, JsonValue]:
    ack: dict[str, JsonValue] = {"v": WIRE_VERSION, "type": "command_ack", "id": command_id,
                                 "status": status}  # fmt: skip
    if reason is not None:
        ack["reason"] = reason
    return ack


@final
class JournalFeed:
    """Хвост журнала → драфты v0. `Translator` — свёртка с первой строки, поэтому читаем с начала,
    а наружу отдаём только строки с `seq > after_seq`."""

    def __init__(self, path: Path, translator: Translator, after_seq: int) -> None:
        self._path = path
        self._translator = translator
        self._after = after_seq
        self._offset = 0

    def poll(self) -> list[tuple[int, tuple[Draft, ...]]]:
        try:
            with self._path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except FileNotFoundError:
            return []
        whole = chunk[: chunk.rfind(b"\n") + 1]  # недописанная строка ждёт следующего опроса
        out: list[tuple[int, tuple[Draft, ...]]] = []
        for line in whole.split(b"\n")[:-1]:
            self._offset += len(line) + 1
            if not line.strip():
                continue
            try:
                event = parse_row(json.loads(line))
                drafts = self._translator.feed(event)
            except ValueError as error:  # ProtocolError — его подкласс; ядовитая строка не должна
                diag("journal_row_skipped", error=str(error))  # останавливать поток навсегда
                continue
            if event.seq > self._after and drafts:
                out.append((event.seq, drafts))
        return out


def command_to_raw(cmd: Command) -> dict[str, JsonValue]:
    """Проверенная команда → файл inbox: лишние поля конверта и мусор отбрасываются."""
    match cmd:
        case MessageCmd():
            return {"id": cmd.id, "kind": "message", "text": cmd.text}
        case ForkAnswerCmd():
            return {"id": cmd.id, "kind": "fork_answer", "fork_id": cmd.fork_id,
                    "answers": dict(cmd.answers)}  # fmt: skip
        case StopCmd():
            return {"id": cmd.id, "kind": "stop"}
        case _:
            assert_never(cmd)


def accept_command(
    frame: Mapping[str, object],
    *,
    inbox_dir: Path,
    fork_questions: Callable[[str], tuple[str, ...] | None],
) -> dict[str, JsonValue]:
    """Команда gateway → `command_ack`. Принятая команда durable (файл в inbox) до ответа."""
    body = {k: v for k, v in frame.items() if k not in FRAME_ENVELOPE}
    raw_id = body.get("id")
    try:
        cmd = parse_command(body)
    except ProtocolError as error:
        return command_ack(raw_id if isinstance(raw_id, str) else None, "rejected", str(error))
    if isinstance(cmd, ForkAnswerCmd):
        questions = fork_questions(cmd.fork_id)
        if questions is None:
            return command_ack(cmd.id, "rejected", "unknown_fork")
        if set(cmd.answers) != set(questions):
            return command_ack(cmd.id, "rejected", "answers_mismatch")
    put(inbox_dir, command_to_raw(cmd))
    return command_ack(cmd.id, "accepted")
