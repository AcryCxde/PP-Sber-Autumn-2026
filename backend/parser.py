import json
from typing import Any, Required, TypedDict

EVENT_SCHEMA_VERSION = 1
MAX_RAW_CONTENT_LENGTH = 4_000
DEFAULT_MAX_FILE_CONTENT_BYTES = 10 * 1024 * 1024


class NormalizedEvent(TypedDict, total=False):
    type: Required[str]
    schema_version: Required[int]
    role: str
    content: Any
    session_id: str | None
    cwd: str | None
    model: str | None
    tools: list[Any]
    agents: list[Any]
    skills: list[Any]
    plugins: list[Any]
    name: str | None
    tool_id: str | None
    input: Any
    status: str
    result_type: str | None
    file: dict[str, Any]
    cost_usd: float | None
    duration_ms: int | None
    num_turns: int | None
    is_error: bool
    result_confirmed: bool
    result: Any
    permission_denials: list[Any]
    subagent_stats: dict[str, Any]
    event_type: str | None
    subtype: str | None
    content_type: str | None
    truncated: bool


def normalized_event(normalized_type: str, **payload: Any) -> NormalizedEvent:
    return {
        "type": normalized_type,
        "schema_version": EVENT_SCHEMA_VERSION,
        **payload,
    }


def bounded_file_content(
    value: Any,
    max_bytes: int = DEFAULT_MAX_FILE_CONTENT_BYTES,
) -> tuple[str | None, int | None, bool]:
    if not isinstance(value, str):
        return None, None, False

    size_bytes = len(value.encode("utf-8"))
    if size_bytes > max_bytes:
        return None, size_bytes, True
    return value, size_bytes, False


def invalid_shape(location: str, value: Any) -> NormalizedEvent:
    return normalized_event(
        "raw_event",
        event_type="invalid_json_shape",
        subtype=location,
        content_type=type(value).__name__,
    )


def parse_event(raw: str) -> list[NormalizedEvent]:
    raw = raw.strip()

    if not raw:
        return []

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return [
            normalized_event(
                "raw",
                content=raw[:MAX_RAW_CONTENT_LENGTH],
                truncated=len(raw) > MAX_RAW_CONTENT_LENGTH,
            )
        ]

    if not isinstance(event, dict):
        return [invalid_shape("event", event)]

    event_type = event.get("type")

    if event_type == "system" and event.get("subtype") == "init":
        return [
            normalized_event(
                "session",
                session_id=event.get("session_id"),
                cwd=event.get("cwd"),
                model=event.get("model"),
                tools=event.get("tools", []),
                agents=event.get("agents", []),
                skills=event.get("skills", []),
                plugins=event.get("plugins", []),
            )
        ]

    if event_type == "assistant":
        events: list[NormalizedEvent] = []
        message = event.get("message", {})
        if not isinstance(message, dict):
            return [invalid_shape("assistant.message", message)]

        content_blocks = message.get("content", [])
        if not isinstance(content_blocks, list):
            return [invalid_shape("assistant.message.content", content_blocks)]

        for content in content_blocks:
            if not isinstance(content, dict):
                events.append(invalid_shape("assistant.content", content))
                continue

            content_type = content.get("type")

            if content_type == "text":
                events.append(
                    normalized_event(
                        "message",
                        role="claude",
                        content=content.get("text", ""),
                    )
                )
            elif content_type == "tool_use":
                events.append(
                    normalized_event(
                        "tool",
                        name=content.get("name"),
                        tool_id=content.get("id"),
                        input=content.get("input", {}),
                        status="running",
                    )
                )
            elif content_type == "thinking":
                events.append(normalized_event("thinking"))
            else:
                events.append(
                    normalized_event(
                        "raw_event",
                        event_type="assistant",
                        content_type=content_type,
                    )
                )

        return events

    if event_type == "user":
        events = []
        message = event.get("message", {})
        if not isinstance(message, dict):
            return [invalid_shape("user.message", message)]

        content_blocks = message.get("content", [])
        if not isinstance(content_blocks, list):
            return [invalid_shape("user.message.content", content_blocks)]

        top_level_tool_result = event.get("tool_use_result")
        top_level_result_malformed = False
        if top_level_tool_result is not None and not isinstance(
            top_level_tool_result, dict
        ):
            events.append(
                invalid_shape("user.tool_use_result", top_level_tool_result)
            )
            top_level_tool_result = None
            top_level_result_malformed = True

        for content in content_blocks:
            if not isinstance(content, dict):
                events.append(invalid_shape("user.content", content))
                continue

            content_type = content.get("type")
            if content_type != "tool_result":
                events.append(
                    normalized_event(
                        "raw_event",
                        event_type="user",
                        content_type=content_type,
                    )
                )
                continue

            block_tool_result = content.get("tool_use_result")
            block_result_malformed = (
                "tool_use_result" in content
                and block_tool_result is not None
                and not isinstance(block_tool_result, dict)
            )
            if block_result_malformed:
                events.append(
                    invalid_shape("user.content.tool_use_result", block_tool_result)
                )
                tool_result = None
            else:
                tool_result = (
                    block_tool_result
                    if isinstance(block_tool_result, dict)
                    else top_level_tool_result
                )

            result_malformed = block_result_malformed or (
                not isinstance(block_tool_result, dict)
                and top_level_result_malformed
            )
            content_error = content.get("is_error")
            mapped_error = (
                tool_result.get("is_error")
                if isinstance(tool_result, dict)
                else None
            )
            explicit_error = (
                content_error
                if isinstance(content_error, bool)
                else mapped_error if isinstance(mapped_error, bool) else None
            )
            result = normalized_event(
                "tool_result",
                tool_id=content.get("tool_use_id"),
                content=content.get("content"),
                is_error=(
                    explicit_error is True
                    or (explicit_error is None and result_malformed)
                ),
                result_confirmed=(
                    explicit_error is not None or not result_malformed
                ),
            )
            if isinstance(tool_result, dict) and tool_result.get("type"):
                result["result_type"] = tool_result.get("type")
            events.append(result)

        return events

    if event_type == "result":
        return [
            normalized_event(
                "result",
                session_id=event.get("session_id"),
                cost_usd=event.get("total_cost_usd"),
                duration_ms=event.get("duration_ms"),
                num_turns=event.get("num_turns"),
                is_error=event.get("is_error", False),
                result=event.get("result"),
                permission_denials=event.get("permission_denials", []),
                subagent_stats=event.get("subagent_stats", {}),
            )
        ]

    return [
        normalized_event(
            "raw_event",
            event_type=event_type,
            subtype=event.get("subtype"),
        )
    ]
