import json


def parse_event(raw: str):
    raw = raw.strip()

    if not raw:
        return None

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "type": "raw",
            "content": raw,
        }

    event_type = event.get("type")

    # ----------------------------------------
    # Session initialization
    # ----------------------------------------

    if event_type == "system" and event.get("subtype") == "init":
        return {
            "type": "session",
            "session_id": event.get("session_id"),
            "cwd": event.get("cwd"),
            "model": event.get("model"),
            "tools": event.get("tools", []),
            "agents": event.get("agents", []),
            "skills": event.get("skills", []),
            "plugins": event.get("plugins", []),
        }

    # ----------------------------------------
    # Claude assistant message
    # ----------------------------------------

    if event_type == "assistant":
        message = event.get("message", {})

        for content in message.get("content", []):
            content_type = content.get("type")

            # Normal Claude text
            if content_type == "text":
                return {
                    "type": "message",
                    "role": "claude",
                    "content": content.get("text", ""),
                }

            # Tool call
            if content_type == "tool_use":
                return {
                    "type": "tool",
                    "name": content.get("name"),
                    "tool_id": content.get("id"),
                    "input": content.get("input", {}),
                    "status": "running",
                }

            # Internal thinking is deliberately not exposed.
            if content_type == "thinking":
                return {
                    "type": "thinking",
                }

    # ----------------------------------------
    # Tool result
    # ----------------------------------------

    if event_type == "user":
        message = event.get("message", {})

        for content in message.get("content", []):
            if content.get("type") != "tool_result":
                continue

            tool_result = content.get("tool_use_result", {})

            result = {
                "type": "tool_result",
                "tool_id": content.get("tool_use_id"),
                "content": content.get("content"),
            }

            # Read/Edit/Write and similar file operations
            if isinstance(tool_result, dict):
                if tool_result.get("type"):
                    result["result_type"] = tool_result.get("type")

                if tool_result.get("file"):
                    file_info = tool_result["file"]

                    result["file"] = {
                        "path": file_info.get("filePath"),
                        "content": file_info.get("content"),
                        "num_lines": file_info.get("numLines"),
                    }

            return result

    # ----------------------------------------
    # Final result
    # ----------------------------------------

    if event_type == "result":
        return {
            "type": "result",
            "session_id": event.get("session_id"),
            "cost_usd": event.get("total_cost_usd"),
            "duration_ms": event.get("duration_ms"),
            "num_turns": event.get("num_turns"),
            "is_error": event.get("is_error", False),
            "result": event.get("result"),
            "permission_denials": event.get("permission_denials", []),
            "subagent_stats": event.get("subagent_stats", {}),
        }

    # ----------------------------------------
    # Unknown JSON event
    # ----------------------------------------

    return {
        "type": "raw_event",
        "event": event,
    }
