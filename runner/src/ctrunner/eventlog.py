"""Журнал событий `events.jsonl`: сначала запись на диск, потом отправка наружу."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Self, final

from ctrunner.protocol import EventKind, JsonValue, question_texts
from ctrunner.redact import Redactor


@final
@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    ts: float
    turn_id: str | None
    kind: EventKind
    payload: JsonValue

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "turn_id": self.turn_id,
            "kind": self.kind.value,
            "payload": self.payload,
        }


@final
class EventLog:
    def __init__(
        self, file: IO[str], last_seq: int, redactor: Redactor, clock: Callable[[], float]
    ) -> None:
        self._file = file
        self._seq = last_seq
        self._redactor = redactor
        self._clock = clock

    @classmethod
    def open(cls, path: Path, redactor: Redactor, clock: Callable[[], float]) -> Self:
        last_seq = _recover(path) if path.exists() else 0
        return cls(path.open("a", encoding="utf-8"), last_seq, redactor, clock)

    def append(self, kind: EventKind, turn_id: str | None, payload: JsonValue) -> Event:
        event = Event(self._seq + 1, self._clock(), turn_id, kind, self._redactor.apply(payload))
        self._file.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._seq = event.seq
        return event

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _recover(path: Path) -> int:
    """Отрезать недописанную последнюю строку (обрыв посреди записи) и вернуть последний seq."""
    good_len, last = 0, b""
    with path.open("rb") as f:
        for line in f:
            if not line.endswith(b"\n"):
                break
            good_len += len(line)
            last = line
    if good_len != path.stat().st_size:
        os.truncate(path, good_len)
    if not last:
        return 0
    seq = json.loads(last).get("seq")
    if not isinstance(seq, int):
        raise ValueError(f"{path}: last event has no integer seq")
    return seq


def kinds_of_turn(path: Path, turn_id: str) -> frozenset[EventKind]:
    """Какие события хода уже в журнале: восстановление дописывает только недостающие.

    `turn_failed{fatal}` не в счёт: это падение процесса, а не итог хода (иначе после краша
    `GiveUp` принял бы его за уже записанный `resume_limit`).
    """
    kinds: set[EventKind] = set()
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if row.get("turn_id") == turn_id and not _is_process_crash(row):
                    kinds.add(EventKind(row["kind"]))
    except FileNotFoundError:
        return frozenset()
    return frozenset(kinds)


@final
@dataclass(frozen=True, slots=True)
class OpenFork:
    fork_id: str
    questions: tuple[str, ...]


def open_forks(path: Path, turn_id: str) -> tuple[OpenFork, ...]:
    """Развилки хода без ответа. `turn_interrupted` обнуляет набор: предыдущее восстановление
    уже обработало их, поэтому при старте процесса остаются ровно вопросы, висевшие при падении."""
    opened: dict[str, OpenFork] = {}
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if row.get("turn_id") != turn_id:
                    continue
                kind, payload = row.get("kind"), row.get("payload")
                if kind == EventKind.TURN_INTERRUPTED:
                    opened.clear()
                elif isinstance(payload, dict) and isinstance(
                    fork_id := payload.get("fork_id"), str
                ):
                    if kind == EventKind.FORK_QUESTION:
                        opened[fork_id] = OpenFork(
                            fork_id, question_texts(payload.get("questions"))
                        )
                    elif kind == EventKind.FORK_ANSWERED:
                        opened.pop(fork_id, None)
    except FileNotFoundError:
        return ()
    return tuple(opened.values())


def open_questions(path: Path, turn_id: str) -> tuple[str, ...]:
    """Тексты неотвеченных вопросов хода без повторов, в порядке появления."""
    return tuple(dict.fromkeys(q for fork in open_forks(path, turn_id) for q in fork.questions))


def _is_process_crash(row: dict[str, object]) -> bool:
    payload = row.get("payload")
    return (
        row.get("kind") == EventKind.TURN_FAILED
        and isinstance(payload, dict)
        and payload.get("fatal") is True
    )
