import time
from uuid import uuid5, NAMESPACE_URL

import pytest

from backend.runner_transport import RunnerTransport, TransportError


def draft(kind="run.started", payload=None, event_id="e1", run="run-1", project="p1"):
    return {"event_id": str(uuid5(NAMESPACE_URL, event_id)), "project_id": project, "run_id": run,
            "occurred_at": "2026-10-16T07:00:00Z", "type": kind,
            "payload": {} if payload is None else payload}


@pytest.fixture
def transport(tmp_path):
    return RunnerTransport(tmp_path / "transport.db")


def test_durable_command_idempotence_and_conflicts(transport):
    expected = transport.message("p1", "run-1", "c1", "go", roles=["reviewer"],
                                 stage="research", input_files=["inputs/a.md"])
    assert transport.message("p1", "run-1", "c1", "go", roles=["reviewer"],
                             stage="research", input_files=["inputs/a.md"]) == expected
    reopened = RunnerTransport(transport.database)
    assert reopened.commands("p1") == [expected]
    with pytest.raises(TransportError, match="idempotency_conflict"):
        transport.message("p2", "run-1", "c1", "go")
    with pytest.raises(TransportError, match="active_run"):
        transport.message("p1", "run-2", "c2", "go")
    transport.ack("p1", {"id": "run-1", "status": "accepted"})
    assert reopened.commands("p1") == []
    assert transport.status("p1", "run-1")["status"] == "queued"  # not a result


def test_cursor_replays_same_journal_row_and_dedups_events(transport):
    transport.message("p1", "run-1", "c1", "go")
    assert transport.hello("p1") == 0
    a = draft()
    b = draft("message.created", {"role_id": None, "text": "ok"}, "e2")
    assert transport.ingest("p1", 2, a)
    assert transport.hello("p1") == 2
    assert not transport.ingest("p1", 2, a)
    assert transport.ingest("p1", 2, b)  # remaining output of SAME journal row
    assert [e["seq"] for e in transport.events("p1", "run-1")] == [1, 2, 3]
    with pytest.raises(TransportError, match="event_id_conflict"):
        transport.ingest("p1", 2, draft("message.created", {"role_id": None, "text": "different"}, "e2"))
    assert len(transport.events("p1", "run-1", 2)) == 1


def test_question_answer_same_run_and_rejected_receipt(transport):
    transport.message("p1", "run-1", "c1", "go")
    q = {"question_id": "q1", "questions": [{"text": "Format?", "header": None,
         "options": [{"label": "md", "description": None}],
         "multi_select": False, "allow_custom": True}]}
    transport.ingest("p1", 2, draft("facilitator.question", q))
    assert transport.status("p1", "run-1")["status"] == "waiting_for_input"
    with pytest.raises(TransportError, match="active_run"):
        transport.message("p1", "run-2", "c2", "go")
    with pytest.raises(TransportError, match="answers_mismatch"):
        transport.answer("p1", "run-1", "a0", "q1", {"wrong": "md"})
    command = transport.answer("p1", "run-1", "a1", "q1", {"Format?": "md"})
    assert command["kind"] == "fork_answer" and command["fork_id"] == "q1"
    transport.ack("p1", {"id": "a1", "status": "rejected", "reason": "unknown_fork"})
    assert transport.ack_receipt("p1", "a1")["reason"] == "unknown_fork"
    assert transport.status("p1", "run-1")["delivery_blocked"]
    assert transport.status("p1", "run-1")["status"] == "waiting_for_input"
    transport.ingest("p1", 3, draft("user.answer.accepted",
        {"question_id": "q1", "answers": {"Format?": "md"}}, "e2"))
    assert transport.status("p1", "run-1")["status"] == "running"


def test_storage_missing_holds_completed_and_confirmation_orders_terminal(transport):
    transport.message("p1", "run-1", "c1", "go")
    artifact = {"artifact_id": "a1", "path": "result.md", "commit_sha": "abc",
                "role_id": None, "size_bytes": 2, "sha256": "a" * 64}
    transport.ingest("p1", 2, draft("artifact.created", artifact))
    completed = draft("run.completed", event_id="e2")
    transport.ingest("p1", 3, completed)
    assert transport.status("p1", "run-1")["awaiting_storage"]
    assert "run.completed" not in [e["type"] for e in transport.events("p1", "run-1")]
    assert not transport.ingest("p1", 3, completed)
    # No fake unknown just because storage isn't configured, even after heartbeat deadline.
    transport.hello("p1")
    assert transport.expire(now=time.time() + 60) == []
    receipt = {"artifact_id": "a1", "version_id": "v1", "size_bytes": 2,
               "download": "/authorized/artifacts/v1"}
    with pytest.raises(TransportError, match="artifact_size_mismatch"):
        transport.confirm_saved("p1", "run-1", "a1", {**receipt, "size_bytes": 3})
    transport.confirm_saved("p1", "run-1", "a1", receipt)
    transport.confirm_saved("p1", "run-1", "a1", receipt)  # no duplicates
    assert [e["type"] for e in transport.events("p1", "run-1")][-2:] == [
        "artifact.saved", "run.completed"]
    assert not transport.ingest("p1", 3, completed)
    with pytest.raises(TransportError):
        transport.ingest("p1", 4, draft("run.started", event_id="e3"))


def test_heartbeat_loss_is_terminal_not_awaiting_answer(transport):
    transport.message("p1", "run-1", "c1", "go")
    transport.hello("p1")
    transport.heartbeat("p1", {"phase": "awaiting_answer", "health": "ok",
                              "last_progress_at": 1.0})
    assert transport.expire(now=time.time() + 29) == []
    assert transport.expire(now=time.time() + 31) == ["run-1"]
    assert transport.events("p1", "run-1")[-1]["payload"] == {"code": "heartbeat_lost"}
    with pytest.raises(TransportError, match="terminal_run"):
        transport.ingest("p1", 4, draft())


@pytest.mark.parametrize("body", [
    draft(project="p2"), draft("artifact.saved"), draft("run.accepted"),
    draft("run.outcome_unknown", {"code": "heartbeat_lost"}),
    draft("message.created", {"text": "ok", "raw_sdk": {}}),
    draft("artifact.created", {"artifact_id": "a", "commit_sha": "abc", "path": "../secret"}),
])
def test_foreign_and_gateway_only_and_private_events_rejected(transport, body):
    transport.message("p1", "run-1", "c1", "go")
    with pytest.raises(TransportError):
        transport.ingest("p1", 2, body)
    assert transport.hello("p1") == 0
    assert len(transport.events("p1", "run-1")) == 1


@pytest.mark.parametrize("body", [
    {**draft(), "event_id": "not-a-uuid"},
    {**draft(), "occurred_at": "2026-10-16T07:00:00+03:00"},
    draft("message.created", {"role_id": {"raw": "internal"}, "text": "ok"}),
    draft("user.answer.accepted", {"question_id": "q1", "answers": {"Q": {"raw": "internal"}}}),
    draft("role.started", {"role_id": "r1", "role_instance_id": "i1", "title": {"raw": "internal"}}),
    draft("run.failed", {"code": {}, "retryable": False}),
    draft("progress.updated", {"stage": []}),
    draft("role.completed", {"role_id": "r1", "role_instance_id": "i1", "outcome": {}}),
    draft("progress.updated", {"stage": "interrupted", "context_restored": {"raw": "internal"}}),
])
def test_invalid_nested_payload_or_envelope_is_not_published(transport, body):
    transport.message("p1", "run-1", "c1", "go")
    with pytest.raises(TransportError):
        transport.ingest("p1", 2, body)
    assert len(transport.events("p1", "run-1")) == 1


def test_other_project_cannot_read_or_ack_and_failure_is_last(transport):
    transport.message("p1", "run-1", "c1", "go")
    with pytest.raises(TransportError, match="unknown_run"):
        transport.events("p2", "run-1")
    with pytest.raises(TransportError, match="unknown_command"):
        transport.ack("p2", {"id": "run-1", "status": "accepted"})
    transport.ingest("p1", 2, draft("run.failed", {"code": "result_error", "retryable": False}))
    with pytest.raises(TransportError, match="terminal_run"):
        transport.ingest("p1", 3, draft(event_id="new"))
    assert transport.events("p1", "run-1")[-1]["type"] == "run.failed"
