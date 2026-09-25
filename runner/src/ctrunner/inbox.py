"""Файловый inbox команд на томе: `sessions send` → `docker exec ctrunner-inbox` → runner.

На этапе 2 тот же `Command` придёт по WebSocket; inbox останется запасным путём без гейтвея.
"""

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, final

from ctrunner.diag import diag
from ctrunner.protocol import Command, EventKind, JsonValue, ProtocolError, parse_command

PROCESSED: Final = "processed"
REJECTED: Final = "rejected"


def put(directory: Path, raw: Mapping[str, JsonValue]) -> Path:
    """Писатель создаёт файл под временным именем и переименовывает: читатель не видит половину."""
    directory.mkdir(parents=True, exist_ok=True)
    # Префикс времени в наносекундах задаёт порядок разбора.
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
    tmp = directory / f".tmp-{name}"
    tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    target = directory / name
    os.replace(tmp, target)
    return target


@final
class Inbox:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._processed = directory / PROCESSED
        self._rejected = directory / REJECTED
        for d in (self._processed, self._rejected):
            d.mkdir(parents=True, exist_ok=True)
        self._seen = {_command_id(p) for p in self._processed.glob("*.json")} - {None}

    def take(self) -> list[Command]:
        commands: list[Command] = []
        for path in sorted(self._dir.glob("[!.]*.json")):
            try:
                command = parse_command(json.loads(path.read_text(encoding="utf-8")))
            except (ProtocolError, ValueError) as error:
                os.replace(path, self._rejected / path.name)
                diag("command_rejected", file=path.name, reason=str(error))
                continue
            os.replace(path, self._processed / path.name)
            if command.id in self._seen:
                diag("command_duplicate", command_id=command.id)
                continue
            self._seen.add(command.id)
            commands.append(command)
        return commands


def _command_id(path: Path) -> str | None:
    try:
        return parse_command(json.loads(path.read_text(encoding="utf-8"))).id
    except (ProtocolError, ValueError):
        return None


def answers_for(events: Path, fork_id: str, labels: Sequence[str]) -> dict[str, str]:
    """Ответы по порядку сопоставляются вопросам развилки из журнала: CLI не просит их текст."""
    questions = _fork_questions(events, fork_id)
    if len(labels) != len(questions):
        raise ValueError(f"fork {fork_id} expects {len(questions)} answers, got {len(labels)}")
    return dict(zip(questions, labels, strict=True))


def _fork_questions(events: Path, fork_id: str) -> list[str]:
    with events.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            payload = row.get("payload")
            if (
                row.get("kind") == EventKind.FORK_QUESTION
                and isinstance(payload, dict)
                and payload.get("fork_id") == fork_id
            ):
                return [_question_text(q) for q in payload.get("questions") or []]
    raise ValueError(f"unknown fork: {fork_id}")


def _question_text(question: object) -> str:
    text = question.get("question") if isinstance(question, dict) else None
    if not isinstance(text, str):
        raise ValueError("fork question has no text")
    return text


def cli() -> None:
    parser = argparse.ArgumentParser(prog="ctrunner-inbox")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("message").add_argument("text")
    fork = sub.add_parser("fork-answer")
    fork.add_argument("fork_id")
    fork.add_argument("labels", nargs="+", help="ответы по порядку вопросов развилки")
    sub.add_parser("stop")
    args = parser.parse_args()

    runner_dir = Path(os.environ.get("WORKSPACE") or "/workspace") / ".runner"
    command_id = uuid.uuid4().hex
    raw: dict[str, JsonValue]
    match args.cmd:
        case "message":
            raw = {"id": command_id, "kind": "message", "text": args.text}
        case "fork-answer":
            try:
                answers = answers_for(runner_dir / "events.jsonl", args.fork_id, args.labels)
            except (OSError, ValueError) as error:
                sys.exit(f"ctrunner-inbox: {error}")
            raw = {
                "id": command_id,
                "kind": "fork_answer",
                "fork_id": args.fork_id,
                "answers": dict(answers),
            }
        case "stop":
            raw = {"id": command_id, "kind": "stop"}
        case other:
            parser.error(f"unknown command {other}")
    print(put(runner_dir / "inbox", raw).name)
