"""Общие типы протокола: JSON, команды к runner и виды событий журнала."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import final

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class ProtocolError(ValueError):
    """Входящее сообщение не соответствует протоколу."""


@final
@dataclass(frozen=True, slots=True)
class MessageCmd:
    id: str
    text: str


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


def _text(raw: Mapping[object, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"field {key!r} must be a non-empty string")
    return value


def _answers(raw: Mapping[object, object]) -> dict[str, str]:
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
    command_id = _text(raw, "id")
    match raw.get("kind"):
        case "message":
            return MessageCmd(command_id, _text(raw, "text"))
        case "fork_answer":
            return ForkAnswerCmd(command_id, _text(raw, "fork_id"), _answers(raw))
        case "stop":
            return StopCmd(command_id)
        case kind:
            raise ProtocolError(f"unknown command kind: {kind!r}")
