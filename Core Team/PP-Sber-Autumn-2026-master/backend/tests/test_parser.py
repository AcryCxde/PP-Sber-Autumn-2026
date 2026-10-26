import json
from pathlib import Path

from backend.parser import MAX_RAW_CONTENT_LENGTH, bounded_file_content, parse_event


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
                        "is_error": False,
                        "tool_use_result": {"type": "text"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-2",
                        "content": "second",
                        "is_error": False,
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
    assert events[0]["result_type"] == "text"
    assert events[1]["result_type"] == "file"
    assert "file" not in events[1]
    assert events[1]["is_error"] is False
    assert events[1]["result_confirmed"] is True


def test_parser_reads_top_level_tool_result_and_error_status() -> None:
    raw = json.dumps(
        {
            "type": "user",
            "tool_use_result": {"type": "text"},
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "write-tool",
                        "content": "Write failed",
                        "is_error": True,
                    }
                ]
            },
        }
    )

    events = parse_event(raw)

    assert events == [
        {
            "type": "tool_result",
            "schema_version": 1,
            "tool_id": "write-tool",
            "content": "Write failed",
            "is_error": True,
            "result_confirmed": True,
            "result_type": "text",
        }
    ]


def test_tool_result_without_error_flag_defaults_to_success() -> None:
    raw = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "write-tool",
                        "content": "File created successfully",
                    }
                ]
            },
        }
    )

    events = parse_event(raw)

    assert events == [
        {
            "type": "tool_result",
            "schema_version": 1,
            "tool_id": "write-tool",
            "content": "File created successfully",
            "is_error": False,
            "result_confirmed": True,
        }
    ]


def test_malformed_tool_result_is_not_confirmed_as_success() -> None:
    raw = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "write-tool",
                        "content": "ambiguous",
                        "tool_use_result": "malformed",
                    }
                ]
            },
        }
    )

    events = parse_event(raw)

    assert [event["type"] for event in events] == ["raw_event", "tool_result"]
    assert events[1]["result_confirmed"] is False
    assert events[1]["is_error"] is True


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
    assert [event["type"] for event in file_events] == ["tool_result"]
    assert "file" not in file_events[0]


def test_large_file_content_is_not_persisted() -> None:
    content, size_bytes, truncated = bounded_file_content("абвг", max_bytes=4)

    assert content is None
    assert size_bytes == 8
    assert truncated is True


def test_stream_json_write_fixture_uses_top_level_tool_result() -> None:
    fixture = Path(__file__).with_name("fixtures") / "stream_json_write.jsonl"
    parsed = [
        event
        for line in fixture.read_text(encoding="utf-8").splitlines()
        for event in parse_event(line)
    ]

    assert [event["type"] for event in parsed] == [
        "session",
        "tool",
        "tool_result",
        "result",
    ]
    assert parsed[2]["tool_id"] == "toolu_fixture_write"
    assert parsed[2]["result_type"] == "text"
    assert parsed[2]["is_error"] is False
    assert parsed[2]["result_confirmed"] is True
    assert "file" not in parsed[2]


def test_parser_returns_empty_list_for_blank_input() -> None:
    assert parse_event("   \n") == []
