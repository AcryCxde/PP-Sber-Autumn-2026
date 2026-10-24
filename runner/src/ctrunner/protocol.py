"""Общие типы протокола: JSON, команды к runner и виды событий журнала."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, final

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class ProtocolError(ValueError):
    """Входящее сообщение не соответствует протоколу."""


@final
@dataclass(frozen=True, slots=True)
class MessageCmd:
    id: str
    text: str
    # Необязательный вход хода: пусто — как раньше, без блока в промпте.
    roles: tuple[str, ...] = ()
    stage: str | None = None
    input_files: tuple[str, ...] = ()  # пути относительно /workspace/project


@final
@dataclass(frozen=True, slots=True)
class ForkAnswerCmd:
    id: str
    fork_id: str
    answers: dict[str, str]


@final
@dataclass(frozen=True, slots=True)
class StopCmd:
    id: str


type Command = MessageCmd | ForkAnswerCmd | StopCmd


class EventKind(StrEnum):
    SESSION_STARTED = "session_started"
    TURN_STARTED = "turn_started"
    SDK = "sdk"
    FORK_QUESTION = "fork_question"
    FORK_ANSWERED = "fork_answered"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    ACCESS_DENIED = "access_denied"
    CHECKPOINT = "checkpoint"
    TURN_INTERRUPTED = "turn_interrupted"
    TURN_RESUMED = "turn_resumed"


def _text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"field {key!r} must be a non-empty string")
    return value


COMMAND_ID_RE: Final = re.compile(r"[0-9A-Za-z_-]{1,64}")


def _command_id(raw: Mapping[str, object]) -> str:
    value = _text(raw, "id")
    if COMMAND_ID_RE.fullmatch(value) is None:
        # id попадает в trailer `Turn-Id` и в `run_id`: перевод строки или пробел его ломает.
        raise ProtocolError("field 'id' must match [0-9A-Za-z_-]{1,64}")
    return value


MAX_ROLES: Final = 32
MAX_LABEL_LEN: Final = 128  # роль и этап
MAX_INPUT_FILES: Final = 64
MAX_INPUT_PATH_LEN: Final = 512
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")


def _label(value: object, key: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_LABEL_LEN:
        raise ProtocolError(f"field {key!r} must be a string of 1..{MAX_LABEL_LEN} characters")
    return value


def _input_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_INPUT_PATH_LEN:
        raise ProtocolError(
            f"field 'input_files' must hold strings of 1..{MAX_INPUT_PATH_LEN} characters"
        )
    if value.startswith("/") or "\\" in value or _CONTROL_RE.search(value) is not None:
        # NUL и переводы строк ломают и файловую систему, и промпт; обратный слэш прячет `..`.
        raise ProtocolError(
            "field 'input_files' must hold relative paths without control characters"
        )
    if ".." in value.split("/"):
        raise ProtocolError("field 'input_files' must not contain '..'")
    return value


def _list_of(raw: Mapping[str, object], key: str, limit: int) -> list[object]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ProtocolError(f"field {key!r} must be an array of at most {limit} items")
    return value


def parse_message(command_id: str, raw: Mapping[str, object]) -> MessageCmd:
    """Граница: `roles`, `stage`, `input_files` необязательны; `null` — то же, что отсутствие."""
    stage = raw.get("stage")
    return MessageCmd(
        command_id,
        _text(raw, "text"),
        tuple(_label(r, "roles") for r in _list_of(raw, "roles", MAX_ROLES)),
        None if stage is None else _label(stage, "stage"),
        tuple(_input_path(p) for p in _list_of(raw, "input_files", MAX_INPUT_FILES)),
    )


def _answers(raw: Mapping[str, object]) -> dict[str, str]:
    value = raw.get("answers")
    if not isinstance(value, dict):
        raise ProtocolError("field 'answers' must be an object")
    answers: dict[str, str] = {}
    for question, answer in value.items():
        if not isinstance(question, str) or not isinstance(answer, str):
            raise ProtocolError("field 'answers' must map strings to strings")
        answers[question] = answer
    return answers


def parse_command(raw: object) -> Command:
    if not isinstance(raw, dict):
        raise ProtocolError("command must be an object")
    command_id = _command_id(raw)
    match raw.get("kind"):
        case "message":
            return parse_message(command_id, raw)
        case "fork_answer":
            return ForkAnswerCmd(command_id, _text(raw, "fork_id"), _answers(raw))
        case "stop":
            return StopCmd(command_id)
        case kind:
            raise ProtocolError(f"unknown command kind: {kind!r}")


def question_texts(raw: JsonValue) -> tuple[str, ...]:
    """Тексты вопросов AskUserQuestion. Ввод от модели: элементы без текста пропускаются."""
    if not isinstance(raw, list):
        return ()
    texts: list[str] = []
    for item in raw:
        text = item.get("question") if isinstance(item, dict) else None
        if isinstance(text, str):
            texts.append(text)
    return tuple(texts)
