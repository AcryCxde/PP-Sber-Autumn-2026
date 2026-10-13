import pytest

from backend.public_events import (
    project_public_event_data,
    validate_public_event_data,
)


def test_public_projection_removes_unapproved_fields() -> None:
    projected = project_public_event_data(
        {
            "type": "run.completed",
            "schema_version": 1,
            "status": "completed",
            "client_request_id": "request-1",
            "session_state": "active",
            "session_id": "secret-session",
            "cwd": "/host/private",
            "cost_usd": 10,
            "permission_denials": ["secret"],
            "subagent_stats": {"secret": 1},
        }
    )

    assert projected == {
        "type": "run.completed",
        "schema_version": 1,
        "status": "completed",
        "client_request_id": "request-1",
        "session_state": "active",
    }


def test_unknown_stored_event_becomes_redacted_placeholder() -> None:
    projected = project_public_event_data(
        {
            "type": "tool_result",
            "schema_version": 1,
            "content": "secret tool output",
        }
    )

    assert projected == {
        "type": "legacy.redacted",
        "schema_version": 1,
        "redacted": True,
    }


def test_unsafe_artifact_path_is_redacted_on_read() -> None:
    projected = project_public_event_data(
        {
            "type": "artifact.saved",
            "schema_version": 1,
            "path": "C:\\host\\secret.txt",
            "size_bytes": 10,
        }
    )

    assert projected == {
        "type": "legacy.redacted",
        "schema_version": 1,
        "redacted": True,
    }


def test_new_internal_event_cannot_be_persisted() -> None:
    with pytest.raises(ValueError, match="Unsupported public event type"):
        validate_public_event_data(
            {
                "type": "tool",
                "schema_version": 1,
                "input": {"content": "secret"},
            }
        )
