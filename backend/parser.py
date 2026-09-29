import json
from typing import Any, Required, TypedDict


EVENT_SCHEMA_VERSION = 1
MAX_RAW_CONTENT_LENGTH = 4_000


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

            tool_result = content.get("tool_use_result", {})
            result = normalized_event(
                "tool_result",
                tool_id=content.get("tool_use_id"),
                content=content.get("content"),
            )

            invalid_tool_shape: NormalizedEvent | None = None
            if isinstance(tool_result, dict):
                if tool_result.get("type"):
                    result["result_type"] = tool_result.get("type")

                if tool_result.get("file"):
                    file_info = tool_result["file"]
                    if isinstance(file_info, dict):
                        result["file"] = {
                            "path": file_info.get("filePath"),
                            "content": file_info.get("content"),
                            "num_lines": file_info.get("numLines"),
                        }
                    else:
                        invalid_tool_shape = invalid_shape(
                            "user.tool_result.file",
                            file_info,
                        )
            elif tool_result is not None:
                invalid_tool_shape = invalid_shape(
                    "user.tool_use_result",
                    tool_result,
                )

            events.append(result)
            if invalid_tool_shape is not None:
                events.append(invalid_tool_shape)

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
