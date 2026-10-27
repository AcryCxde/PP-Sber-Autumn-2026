from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Required, TypedDict


EVENT_SCHEMA_VERSION = 1

PUBLIC_EVENT_TYPES = {
    "run.accepted",
    "run.started",
    "context.reset",
    "message.created",
    "facilitator.question",
    "user.answer.accepted",
    "artifact.saved",
    "run.completed",
    "run.failed",
    "run.outcome_unknown",
    "legacy.redacted",
}

TERMINAL_EVENT_TYPES = {
    "run.completed",
    "run.failed",
    "run.outcome_unknown",
}
SAFE_FAILURE_CODES = {
    "artifact_save_failed",
    "backend_task_cancelled",
    "claude_cli_failed",
    "claude_request_failed",
    "claude_session_mismatch",
    "claude_session_unavailable",
    "result_error",
}
SAFE_UNKNOWN_CODES = {
    "backend_restarted",
    "heartbeat_lost",
    "claude_session_state_uncertain",
}
SAFE_CONTEXT_STATES = {"new", "active", "reset", "unavailable"}


class PublicEventDraft(TypedDict):
    type: Required[str]
    schema_version: Required[int]


def _safe_relative_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        return None
    if value.startswith("/") or (len(value) >= 2 and value[1] == ":"):
        return None
    parts = value.split("/")
    if any(
        not part or part in {".", "..", ".git", ".runner"}
        for part in parts
    ):
        return None
    return value


def _safe_message(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    result: dict[str, Any] = {}
    for key in (
        "id",
        "conversation_id",
        "run_id",
        "role",
        "kind",
        "sequence",
        "content",
        "created_at",
    ):
        item = value.get(key)
        if isinstance(item, (str, int)) or item is None:
            result[key] = item
    return result


def project_public_event_data(event_data: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(event_data.get("type", ""))
    schema_version = event_data.get("schema_version")
    if not isinstance(schema_version, int):
        schema_version = EVENT_SCHEMA_VERSION

    projected: dict[str, Any] = {
        "type": event_type,
        "schema_version": schema_version,
    }

    if event_type == "run.accepted":
        for key in ("client_request_id", "context_mode"):
            value = event_data.get(key)
            if isinstance(value, str):
                projected[key] = value
        message = _safe_message(event_data.get("message"))
        if message is not None:
            projected["message"] = message
    elif event_type == "run.started":
        pass
    elif event_type == "context.reset":
        message = _safe_message(event_data.get("message"))
        if message is not None:
            projected["message"] = message
    elif event_type == "message.created":
        role = event_data.get("role")
        content = event_data.get("content")
        if isinstance(role, str):
            projected["role"] = role
        if isinstance(content, str):
            projected["content"] = content
    elif event_type == "facilitator.question":
        question_id = event_data.get("question_id")
        questions = event_data.get("questions")
        if not isinstance(question_id, str) or not question_id or not isinstance(questions, list):
            return {"type": "legacy.redacted", "schema_version": EVENT_SCHEMA_VERSION, "redacted": True}
        projected["question_id"] = question_id
        projected["questions"] = questions
    elif event_type == "user.answer.accepted":
        question_id = event_data.get("question_id")
        answers = event_data.get("answers")
        if not isinstance(question_id, str) or not question_id or not isinstance(answers, Mapping):
            return {"type": "legacy.redacted", "schema_version": EVENT_SCHEMA_VERSION, "redacted": True}
        projected["question_id"] = question_id
        projected["answers"] = dict(answers)
    elif event_type == "artifact.saved":
        path = _safe_relative_path(event_data.get("path"))
        if path is None:
            return {
                "type": "legacy.redacted",
                "schema_version": EVENT_SCHEMA_VERSION,
                "redacted": True,
            }
        projected["path"] = path
        for key in ("size_bytes", "num_lines"):
            value = event_data.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                projected[key] = value
        projected["truncated"] = event_data.get("truncated") is True
    elif event_type == "run.completed":
        projected["status"] = "completed"
        client_request_id = event_data.get("client_request_id")
        if isinstance(client_request_id, str):
            projected["client_request_id"] = client_request_id
        session_state = event_data.get("session_state")
        if session_state in SAFE_CONTEXT_STATES:
            projected["session_state"] = session_state
        message = _safe_message(event_data.get("assistant_message"))
        if message is not None:
            projected["assistant_message"] = message
    elif event_type in {"run.failed", "run.outcome_unknown"}:
        session_state = event_data.get("session_state")
        if session_state in SAFE_CONTEXT_STATES:
            projected["session_state"] = session_state
        if event_type == "run.failed":
            status = event_data.get("status")
            projected["status"] = (
                status if status in {"failed", "cancelled"} else "failed"
            )
            code = event_data.get("error_code")
            projected["error_code"] = (
                code if code in SAFE_FAILURE_CODES else "claude_request_failed"
            )
            projected["message"] = "Запрос Claude завершился с ошибкой."
        else:
            projected["status"] = "outcome_unknown"
            code = event_data.get("error_code")
            projected["error_code"] = (
                code if code in SAFE_UNKNOWN_CODES else "heartbeat_lost"
            )
            projected["message"] = "Не удалось определить результат запуска."
        projected["retryable"] = event_data.get("retryable") is True
        projected["can_reset_context"] = (
            event_data.get("can_reset_context") is True
        )
    elif event_type == "legacy.redacted":
        projected["redacted"] = True
    else:
        return {
            "type": "legacy.redacted",
            "schema_version": EVENT_SCHEMA_VERSION,
            "redacted": True,
        }

    return projected


def validate_public_event_data(event_data: Mapping[str, Any]) -> dict[str, Any]:
    event_type = event_data.get("type")
    if event_type not in PUBLIC_EVENT_TYPES:
        raise ValueError(f"Unsupported public event type: {event_type!r}")
    projected = project_public_event_data(event_data)
    if projected["type"] != event_type:
        raise ValueError(f"Unsafe public event payload for type: {event_type!r}")
    return projected


def run_accepted_event(
    *,
    client_request_id: str,
    context_mode: str,
    message: Mapping[str, Any],
) -> dict[str, Any]:
    return validate_public_event_data(
        {
            "type": "run.accepted",
            "schema_version": EVENT_SCHEMA_VERSION,
            "client_request_id": client_request_id,
            "context_mode": context_mode,
            "message": dict(message),
        }
    )


def context_reset_event(message: Mapping[str, Any]) -> dict[str, Any]:
    return validate_public_event_data(
        {
            "type": "context.reset",
            "schema_version": EVENT_SCHEMA_VERSION,
            "message": dict(message),
        }
    )


def run_started_event() -> dict[str, Any]:
    return {
        "type": "run.started",
        "schema_version": EVENT_SCHEMA_VERSION,
    }


def message_created_event(content: str, *, role: str = "claude") -> dict[str, Any]:
    return validate_public_event_data(
        {
            "type": "message.created",
            "schema_version": EVENT_SCHEMA_VERSION,
            "role": role,
            "content": content,
        }
    )


def facilitator_question_event(question_id: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
    return validate_public_event_data({"type": "facilitator.question", "schema_version": EVENT_SCHEMA_VERSION,
                                       "question_id": question_id, "questions": questions})


def user_answer_accepted_event(question_id: str, answers: Mapping[str, str]) -> dict[str, Any]:
    return validate_public_event_data({"type": "user.answer.accepted", "schema_version": EVENT_SCHEMA_VERSION,
                                       "question_id": question_id, "answers": dict(answers)})


def artifact_saved_event(
    *,
    path: str,
    size_bytes: int | None,
    truncated: bool,
    num_lines: int | None,
) -> dict[str, Any]:
    return validate_public_event_data(
        {
            "type": "artifact.saved",
            "schema_version": EVENT_SCHEMA_VERSION,
            "path": path,
            "size_bytes": size_bytes,
            "truncated": truncated,
            "num_lines": num_lines,
        }
    )


def legacy_redacted_event() -> dict[str, Any]:
    return {
        "type": "legacy.redacted",
        "schema_version": EVENT_SCHEMA_VERSION,
        "redacted": True,
    }
