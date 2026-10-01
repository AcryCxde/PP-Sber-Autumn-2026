"""Журнал событий `events.jsonl`: сначала запись на диск, потом отправка наружу."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Self, final

from ctrunner.protocol import EventKind, JsonValue
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
    """Какие события хода уже в журнале: восстановление дописывает только недостающие."""
    kinds: set[EventKind] = set()
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if row.get("turn_id") == turn_id:
                    kinds.add(EventKind(row["kind"]))
    except FileNotFoundError:
        return frozenset()
    return frozenset(kinds)
