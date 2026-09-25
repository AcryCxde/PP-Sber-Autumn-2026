"""Нормализация сообщений SDK в JSON журнала (формат как у `claude --output-format stream-json`)."""

import dataclasses
import json
from typing import assert_never

from claude_agent_sdk import (
    AssistantMessage,
    ConversationResetMessage,
    Message,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    UserMessage,
)

from ctrunner.protocol import JsonValue


def normalize(msg: Message) -> dict[str, JsonValue] | None:  # noqa: PLR0911 — по ветке на вариант
    """`None` — сообщение наружу не уходит (потоковые токены служат только признаком прогресса)."""
    match msg:
        case AssistantMessage():
            return {
                "type": "assistant",
                "parent_tool_use_id": msg.parent_tool_use_id,
                "message": {"model": msg.model, "content": _to_json(_asdict(msg)["content"])},
            }
        case UserMessage():
            return {
                "type": "user",
                "parent_tool_use_id": msg.parent_tool_use_id,
                "message": {"content": _to_json(_asdict(msg)["content"])},
            }
        case SystemMessage():
            # Системные события SDK уже в формате CLI.
            return _to_json_object(msg.data)
        case ResultMessage():
            return {"type": "result", **_to_json_object(_asdict(msg))}
        case RateLimitEvent():
            return {"type": "rate_limit", **_to_json_object(_asdict(msg))}
        case ConversationResetMessage():
            return {"type": "conversation_reset", **_to_json_object(_asdict(msg))}
        case StreamEvent():
            return None
        case _:
            assert_never(msg)


def session_id_of(msg: Message) -> str | None:
    match msg:
        case ResultMessage() | StreamEvent() | RateLimitEvent() | ConversationResetMessage():
            return msg.session_id
        case AssistantMessage():
            return msg.session_id
        case SystemMessage():
            value = msg.data.get("session_id")
            return value if isinstance(value, str) else None
        case UserMessage():
            return None
        case _:
            assert_never(msg)


def _asdict(msg: Message) -> dict[str, object]:
    return dataclasses.asdict(msg)


def _to_json(value: object) -> JsonValue:
    # Единственная граница: всё непредставимое в JSON (datetime, Path…) становится строкой.
    parsed: JsonValue = json.loads(json.dumps(value, default=str))
    return parsed


def _to_json_object(value: object) -> dict[str, JsonValue]:
    parsed = _to_json(value)
    if not isinstance(parsed, dict):
        raise TypeError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
