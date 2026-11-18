"""Журнал runner → публичные события events v0 (`docs/contracts/events-v0.md`).

Перевод — свёртка журнала с начала: одинаковый журнал даёт одинаковые `event_id`,
поэтому повторная отправка после переподключения дедуплицируется по `event_id`.
`seq` присваивает владелец ленты (gateway) при сохранении; `Sequencer` делает то же для fixture.
"""

import json
import math
import posixpath
import re
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, assert_never, final

from ctrunner.config import DEFAULT_WORKSPACE
from ctrunner.eventlog import Event
from ctrunner.protocol import EventKind, JsonValue, ProtocolError
from ctrunner.turn_lifecycle import FailReason, LifecycleChange, TurnLiveness

SCHEMA: Final = "events.v0"
# Постоянное пространство имён: от него зависят все event_id, менять нельзя.
_NAMESPACE: Final = uuid.UUID("5b0f6c1e-6f0c-4a39-9d55-2f4b3d7a0e01")
DEFAULT_PROJECT_ROOT: Final = f"{DEFAULT_WORKSPACE}/project"

_ROLE_TOOLS: Final = frozenset({"Agent", "Task"})  # инструмент субагента: Agent, до CLI 2.1 — Task
_WRITE_TOOLS: Final = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
                       "NotebookEdit": "notebook_path"}  # fmt: skip
_DEFAULT_ROLE: Final = "general-purpose"


class EventType(StrEnum):
    RUN_ACCEPTED = "run.accepted"  # gateway
    RUN_STARTED = "run.started"
    ROLE_STARTED = "role.started"
    ROLE_COMPLETED = "role.completed"
    MESSAGE_CREATED = "message.created"
    FACILITATOR_QUESTION = "facilitator.question"
    USER_ANSWER_ACCEPTED = "user.answer.accepted"
    PROGRESS_UPDATED = "progress.updated"
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_SAVED = "artifact.saved"  # gateway после подтверждения хранилища 2.3
    USAGE_REPORTED = "usage.reported"  # факты `result` SDK, перед терминальным событием
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_OUTCOME_UNKNOWN = "run.outcome_unknown"  # gateway; runner — только при state_corrupt


TERMINAL: Final = frozenset(
    {EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_OUTCOME_UNKNOWN}
)


class Stage(StrEnum):
    INTERRUPTED = "interrupted"  # процесс runner упал посреди хода
    RECOVERING = "recovering"  # runner поднялся и сверяет прерванный ход
    WORKING = "working"  # ход продолжен после восстановления


@final
@dataclass(frozen=True, slots=True)
class Draft:
    """Публичное событие до присвоения `seq`."""

    event_id: str
    project_id: str
    run_id: str
    occurred_at: datetime
    type: EventType
    payload: dict[str, JsonValue]

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")


@final
@dataclass(frozen=True, slots=True)
class PublicEvent:
    seq: int
    draft: Draft

    def to_json(self) -> dict[str, JsonValue]:
        d = self.draft
        return {
            "schema": SCHEMA,
            "event_id": d.event_id,
            "project_id": d.project_id,
            "run_id": d.run_id,
            "seq": self.seq,
            "occurred_at": d.occurred_at.isoformat().replace("+00:00", "Z"),
            "type": d.type.value,
            "payload": d.payload,
        }


def parse_row(raw: object) -> Event:
    """Строка `events.jsonl` → событие журнала. Граница: дальше значения уже проверены."""
    if not isinstance(raw, dict):
        raise ProtocolError("journal row must be an object")
    seq, ts, turn_id, kind = raw.get("seq"), raw.get("ts"), raw.get("turn_id"), raw.get("kind")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise ProtocolError(f"bad seq: {seq!r}")
    if not isinstance(ts, int | float) or isinstance(ts, bool):
        raise ProtocolError(f"bad ts: {ts!r}")
    if turn_id is not None and not isinstance(turn_id, str):
        raise ProtocolError(f"bad turn_id: {turn_id!r}")
    if not isinstance(kind, str) or kind not in EventKind:
        raise ProtocolError(f"unknown kind: {kind!r}")
    return Event(seq, float(ts), turn_id, EventKind(kind), raw.get("payload"))


@dataclass(slots=True)
class _Write:
    path: str
    role_id: str | None


@dataclass(slots=True)
class _Run:
    roles: dict[str, str] = field(default_factory=dict)  # tool_use_id → role_id, активные
    writes: dict[str, _Write] = field(default_factory=dict)  # tool_use_id → запись без ответа
    written: dict[str, _Write] = field(default_factory=dict)  # path → успешная запись


@final
class Translator:
    """Свёртка журнала одного проекта. Кормить строго по порядку `seq`, с первой строки."""

    def __init__(self, project_id: str, project_root: str = DEFAULT_PROJECT_ROOT) -> None:
        self._project_id = project_id
        self._root = project_root.rstrip("/")
        self._runs: dict[str, _Run] = {}
        self._liveness = TurnLiveness()

    def feed(self, event: Event) -> tuple[Draft, ...]:  # noqa: PLR0912 — по ветке на вид
        out = _Out(self._project_id, event)
        change = self._liveness.feed(event)
        match event.kind:
            case EventKind.SESSION_STARTED | EventKind.ACCESS_DENIED:
                pass  # служебное и аудит: в публичную ленту не идут
            case EventKind.TURN_STARTED:
                if change.started is not None:
                    self._runs[change.started] = _Run()
                    out.emit(change.started, EventType.RUN_STARTED, {})
            case EventKind.SDK:
                self._sdk(out, event)
            case EventKind.FORK_QUESTION:
                if (run_id := self._live(event)) is not None:
                    out.emit(run_id, EventType.FACILITATOR_QUESTION, _question(event.payload))
            case EventKind.FORK_ANSWERED:
                if (run_id := self._live(event)) is not None:
                    payload = _object(event.payload)
                    out.emit(run_id, EventType.USER_ANSWER_ACCEPTED, {
                        "question_id": _str(payload, "fork_id"),
                        "answers": _answers(payload.get("answers")),
                    })  # fmt: skip
            case EventKind.CHECKPOINT:
                self._checkpoint(out, event)
            case EventKind.TURN_COMPLETED:
                for run_id in change.closed:
                    out.emit(run_id, EventType.RUN_COMPLETED, {})
            case EventKind.TURN_FAILED:
                self._failed(out, change)
            case EventKind.TURN_INTERRUPTED:
                if (run_id := self._live(event)) is not None:
                    out.emit(run_id, EventType.PROGRESS_UPDATED, {"stage": Stage.RECOVERING.value})
            case EventKind.TURN_RESUMED:
                if (run_id := self._live(event)) is not None:
                    payload = _object(event.payload)
                    out.emit(run_id, EventType.PROGRESS_UPDATED, {
                        "stage": Stage.WORKING.value,
                        "attempt": _int(payload, "attempt"),
                        "context_restored": _str(payload, "mode") == "resume",
                    })  # fmt: skip
            case _:
                assert_never(event.kind)
        return tuple(out.drafts)

    def _live(self, event: Event) -> str | None:
        """Ход, который ещё не получил терминальное событие; иначе событие не публикуется."""
        return event.turn_id if self._liveness.live(event.turn_id) else None

    def _sdk(self, out: "_Out", event: Event) -> None:
        if (run_id := self._live(event)) is None:
            return
        run, msg = self._runs[run_id], _object(event.payload)
        if msg.get("type") == "result":
            out.drafts.append(usage_draft(self._project_id, event))
            return
        parent = msg.get("parent_tool_use_id")
        author = run.roles.get(parent) if isinstance(parent, str) else None
        message = msg.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return  # result, system, rate_limit: внутренние, content нет
        for block in (b for b in blocks if isinstance(b, dict)):
            match msg.get("type"):
                case "assistant":
                    self._assistant_block(out, run_id, run, author, block)
                case "user":
                    self._result_block(out, run_id, run, block)
                case _:
                    pass

    def _assistant_block(
        self,
        out: "_Out",
        run_id: str,
        run: _Run,
        author: str | None,
        block: Mapping[str, JsonValue],
    ) -> None:
        text, name, tool_id, tool_input = (
            block.get("text"), block.get("name"), block.get("id"), block.get("input")
        )  # fmt: skip
        if isinstance(text, str):
            if text.strip():
                out.emit(run_id, EventType.MESSAGE_CREATED, {"role_id": author, "text": text})
            return
        if not isinstance(name, str) or not isinstance(tool_id, str):
            return  # thinking и прочее
        args = tool_input if isinstance(tool_input, dict) else {}
        if name in _ROLE_TOOLS:
            role = args.get("subagent_type")
            role_id = role if isinstance(role, str) and role else _DEFAULT_ROLE
            run.roles[tool_id] = role_id
            title = args.get("description")
            out.emit(run_id, EventType.ROLE_STARTED, {
                "role_id": role_id,
                "role_instance_id": self._opaque(tool_id),
                "title": title if isinstance(title, str) else None,
            })  # fmt: skip
        elif (key := _WRITE_TOOLS.get(name)) is not None:
            path = self._public_path(args.get(key))
            if path is not None:
                run.writes[tool_id] = _Write(path, author)

    def _result_block(
        self, out: "_Out", run_id: str, run: _Run, block: Mapping[str, JsonValue]
    ) -> None:
        tool_id = block.get("tool_use_id")
        if not isinstance(tool_id, str):
            return
        failed = block.get("is_error") is True
        if (role_id := run.roles.pop(tool_id, None)) is not None:
            out.emit(run_id, EventType.ROLE_COMPLETED, {
                "role_id": role_id,
                "role_instance_id": self._opaque(tool_id),
                "outcome": "failed" if failed else "completed",
            })  # fmt: skip
        if (write := run.writes.pop(tool_id, None)) is not None and not failed:
            run.written[write.path] = write

    def _checkpoint(self, out: "_Out", event: Event) -> None:
        if (run_id := self._live(event)) is None:
            return
        payload = _object(event.payload)
        run, sha, files = self._runs[run_id], _str(payload, "sha"), payload.get("files")
        written = dict(run.written)
        # Файлы, созданные не через Write/Edit (скриптом, конвертером), тоже результат работы:
        # их видно по коммиту. Входные материалы пользователя (materials/) результатом не считаются.
        if isinstance(files, dict):
            for path in files:
                if path not in written and _generated(path):
                    written[path] = _Write(path, None)
        for path, write in sorted(written.items()):
            created: dict[str, JsonValue] = {
                "artifact_id": self._opaque(f"{sha}:{path}"),
                "path": path,
                "commit_sha": sha,
                "role_id": write.role_id,
            }
            fact = files.get(path) if isinstance(files, dict) else None
            # Факт добавляется только целым: нет или битый — поля отсутствуют, а не 0 и не "".
            created.update(_file_fact(fact))
            out.emit(run_id, EventType.ARTIFACT_CREATED, created)
        run.written.clear()

    def _failed(self, out: "_Out", change: LifecycleChange) -> None:
        reason = change.reason
        if reason is FailReason.STATE_CORRUPT:
            for run_id in change.closed:
                out.emit(run_id, EventType.RUN_OUTCOME_UNKNOWN, {"code": reason.value})
        elif change.interrupted is not None:
            out.emit(change.interrupted, EventType.PROGRESS_UPDATED,
                     {"stage": Stage.INTERRUPTED.value})
        elif change.closed and reason is not None:
            for run_id in change.closed:
                out.emit(run_id, EventType.RUN_FAILED,
                         {"code": reason.value, "retryable": _retryable(reason)})

    def _public_path(self, raw: JsonValue | None) -> str | None:
        """Путь относительно корня проекта; всё вне проекта (служебные каталоги) не публикуется."""
        if not isinstance(raw, str):
            return None
        full = posixpath.normpath(raw if raw.startswith("/") else f"{self._root}/{raw}")
        if not full.startswith(self._root + "/"):
            return None
        rel = full.removeprefix(self._root + "/")
        return None if rel.split("/")[0] == ".git" else rel

    def _opaque(self, name: str) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{self._project_id}:{name}"))


@final
class _Out:
    def __init__(self, project_id: str, source: Event) -> None:
        self._project_id = project_id
        self._source = source
        self.drafts: list[Draft] = []

    def emit(self, run_id: str, type_: EventType, payload: dict[str, JsonValue]) -> None:
        # Стабилен при повторном переводе; run_id различает журналы пересозданного тома.
        key = f"{self._project_id}:{run_id}:{self._source.seq}:{len(self.drafts)}"
        self.drafts.append(Draft(
            str(uuid.uuid5(_NAMESPACE, key)), self._project_id, run_id,
            datetime.fromtimestamp(self._source.ts, UTC), type_, payload,
        ))  # fmt: skip


def usage_draft(project_id: str, event: Event) -> Draft:
    """Identity and payload shared by public translation and the private usage-only fold.

    A result row emits exactly one draft, at ordinal zero. Liveness is checked by each fold.
    """
    if event.turn_id is None:
        raise ProtocolError("usage requires a turn_id")
    out = _Out(project_id, event)
    out.emit(event.turn_id, EventType.USAGE_REPORTED, _usage(_object(event.payload)))
    return out.drafts[0]


@final
class Sequencer:
    """`seq` на запуск с 1 без пропусков и дедупликация по `event_id` — как в gateway."""

    def __init__(self) -> None:
        self._last: dict[str, int] = {}
        self._seen: set[str] = set()

    def assign(self, drafts: Iterable[Draft]) -> list[PublicEvent]:
        out: list[PublicEvent] = []
        for draft in drafts:
            if draft.event_id in self._seen:
                continue
            self._seen.add(draft.event_id)
            seq = self._last.get(draft.run_id, 0) + 1
            self._last[draft.run_id] = seq
            out.append(PublicEvent(seq, draft))
        return out


def translate(
    project_id: str, rows: Iterable[Event], project_root: str = DEFAULT_PROJECT_ROOT
) -> list[Draft]:
    tr = Translator(project_id, project_root)
    return [draft for row in rows for draft in tr.feed(row)]


def _retryable(reason: FailReason) -> bool:
    match reason:
        case FailReason.RESULT_ERROR | FailReason.CHECKPOINT_FAILED | FailReason.RESUME_LIMIT:
            return True
        case FailReason.DISK_FULL | FailReason.SDK_CRASHED | FailReason.STATE_CORRUPT:
            return False  # нужен оператор
        case _:
            assert_never(reason)


def _question(raw: JsonValue) -> dict[str, JsonValue]:
    """AskUserQuestion → вопрос фасилитатора. Ввод от модели: битые поля отбрасываются."""
    payload = _object(raw)
    items = payload.get("questions")
    questions: list[JsonValue] = []
    for q in items if isinstance(items, list) else []:
        if not isinstance(q, dict) or not isinstance(text := q.get("question"), str):
            continue
        header, options = q.get("header"), q.get("options")
        questions.append({
            "text": text,
            "header": header if isinstance(header, str) else None,
            "options": [_option(o) for o in options if isinstance(o, dict)]
            if isinstance(options, list) else [],
            "multi_select": q.get("multiSelect") is True,
            "allow_custom": True,
        })  # fmt: skip
    return {"question_id": _str(payload, "fork_id"), "questions": questions}


def _count(raw: JsonValue | None) -> int | None:
    """Счётчик из ответа SDK: неизвестное или негодное — `None`, а не 0."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def _cost(raw: JsonValue | None) -> int | float | None:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int | float)
        or not math.isfinite(raw)
        or raw < 0
    ):
        return None
    return raw


_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")


def _generated(path: str) -> bool:
    """Файл из коммита, который публикуется артефактом: не материалы и не служебный файл."""
    parts = path.split("/")
    return parts[0] != "materials" and not any(part.startswith(".") for part in parts)


def _file_fact(raw: JsonValue | None) -> dict[str, JsonValue]:
    """Запись `files[path]` журнала → `size_bytes` и `sha256`; негодная запись — пустой словарь."""
    if not isinstance(raw, dict):
        return {}
    size, digest = _count(raw.get("size_bytes")), raw.get("sha256")
    if size is None or not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        return {}
    return {"size_bytes": size, "sha256": digest}


def _usage(result: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """`result` SDK → `usage.reported`. Только то, что SDK сообщил; прочее наружу не идёт."""
    raw = result.get("usage")
    usage = raw if isinstance(raw, dict) else {}
    return {
        "input_tokens": _count(usage.get("input_tokens")),
        "output_tokens": _count(usage.get("output_tokens")),
        "cache_creation_input_tokens": _count(usage.get("cache_creation_input_tokens")),
        "cache_read_input_tokens": _count(usage.get("cache_read_input_tokens")),
        "num_turns": _count(result.get("num_turns")),
        "duration_ms": _count(result.get("duration_ms")),
        "duration_api_ms": _count(result.get("duration_api_ms")),
        "cost_usd": _cost(result.get("total_cost_usd")),
    }


def _option(raw: Mapping[str, JsonValue]) -> JsonValue:
    label, description = raw.get("label"), raw.get("description")
    return {
        "label": label if isinstance(label, str) else "",
        "description": description if isinstance(description, str) else None,
    }


def _answers(raw: JsonValue | None) -> dict[str, JsonValue]:
    if not isinstance(raw, dict):
        raise ProtocolError("answers must be an object")
    return {str(k): v for k, v in raw.items() if isinstance(v, str)}


def _object(raw: JsonValue | None) -> dict[str, JsonValue]:
    if not isinstance(raw, dict):
        raise ProtocolError("payload must be an object")
    return raw


def _str(raw: Mapping[str, JsonValue], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"field {key!r} must be a non-empty string")
    return value


def _int(raw: Mapping[str, JsonValue], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"field {key!r} must be an integer")
    return value


def read_journal(lines: Iterable[str]) -> list[Event]:
    return [parse_row(json.loads(line)) for line in lines if line.strip()]


def cli(argv: Sequence[str] | None = None) -> int:
    """`ctrunner-v0 <project_id> [project_root] < events.jsonl` → лента v0 (JSONL) на stdout."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not 1 <= len(args) <= 2:
        print(cli.__doc__, file=sys.stderr)
        return 64
    drafts = translate(args[0], read_journal(sys.stdin), *args[1:])
    for event in Sequencer().assign(drafts):
        print(json.dumps(event.to_json(), ensure_ascii=False))
    return 0
