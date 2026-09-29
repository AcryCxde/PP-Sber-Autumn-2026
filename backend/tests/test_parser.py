import json

from backend.parser import MAX_RAW_CONTENT_LENGTH, parse_event


def test_parser_preserves_all_assistant_blocks_in_order() -> None:
    raw = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Before tool"},
                    {"type": "thinking", "thinking": "hidden"},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    },
                    {"type": "text", "text": "After tool"},
                ]
            },
        }
    )

    events = parse_event(raw)

    assert [event["type"] for event in events] == [
        "message",
        "thinking",
        "tool",
        "message",
    ]
    assert events[0]["content"] == "Before tool"
    assert "thinking" not in events[1]
    assert events[2]["tool_id"] == "tool-1"
    assert events[2]["input"] == {"file_path": "README.md"}
    assert events[3]["content"] == "After tool"
    assert all(event["schema_version"] == 1 for event in events)


def test_parser_preserves_multiple_tool_results() -> None:
    raw = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "first",
                        "tool_use_result": {"type": "text"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-2",
                        "content": "second",
                        "tool_use_result": {
                            "type": "file",
                            "file": {
                                "filePath": "example.py",
                                "content": "print('ok')",
                                "numLines": 1,
                            },
                        },
                    },
                ]
            },
        }
    )

    events = parse_event(raw)

    assert [event["tool_id"] for event in events] == ["tool-1", "tool-2"]
    assert events[1]["file"] == {
        "path": "example.py",
        "content": "print('ok')",
        "num_lines": 1,
    }


def test_parser_normalizes_malformed_and_unknown_events_safely() -> None:
    raw_content = "x" * (MAX_RAW_CONTENT_LENGTH + 10)
    malformed = parse_event(raw_content)
    unknown = parse_event(
        json.dumps(
            {
                "type": "future_event",
                "secret": "not copied to normalized payload",
            }
        )
    )

    assert malformed == [
        {
            "type": "raw",
            "schema_version": 1,
            "content": "x" * MAX_RAW_CONTENT_LENGTH,
            "truncated": True,
        }
    ]
    assert unknown == [
        {
            "type": "raw_event",
            "schema_version": 1,
            "event_type": "future_event",
            "subtype": None,
        }
    ]


def test_parser_normalizes_valid_json_with_invalid_shapes() -> None:
    root_events = parse_event("null")
    assistant_events = parse_event(
        json.dumps({"type": "assistant", "message": None})
    )
    content_events = parse_event(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [42, {"type": "text", "text": "ok"}]},
            }
        )
    )
    file_events = parse_event(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "done",
                            "tool_use_result": {"file": "invalid"},
                        }
                    ]
                },
            }
        )
    )

    assert root_events[0]["event_type"] == "invalid_json_shape"
    assert root_events[0]["subtype"] == "event"
    assert assistant_events[0]["subtype"] == "assistant.message"
    assert [event["type"] for event in content_events] == [
        "raw_event",
        "message",
    ]
    assert [event["type"] for event in file_events] == [
        "tool_result",
        "raw_event",
    ]
    assert file_events[1]["subtype"] == "user.tool_result.file"


def test_parser_returns_empty_list_for_blank_input() -> None:
    assert parse_event("   \n") == []
