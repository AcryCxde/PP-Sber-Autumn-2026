"""Independent durable private receipts do not alter public delivery."""
from uuid import uuid4

import pytest

from backend.runner_transport import RunnerTransport, TransportError


def event(kind, payload=None):
    return {"event_id": str(uuid4()), "project_id": "p1", "run_id": "run-1",
            "occurred_at": "2026-11-12T00:00:00Z", "type": kind, "payload": payload or {}}


def usage():
    return event("usage.reported", dict.fromkeys([
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
        "num_turns", "duration_ms", "duration_api_ms", "cost_usd"], None))


def frame(source, seq=2, value="0.062745"):
    from backend.harness_diagnostics import diagnostic_identity
    return {"v": 1, "type": "harness.diagnostic", "schema": "harness.diagnostic.v1",
            "diagnostic_id": diagnostic_identity("p1", "run-1", source["event_id"]),
            "project_id": "p1", "transport_run_id": "run-1",
            "source_event_id": source["event_id"], "source_journal_seq": seq,
            "payload": {"session_ref": "fd81d33f-1427-424b-a030-0065468064f7",
                        "observed_at": source["occurred_at"], "value_usd": value}}


def test_diagnostic_identity_golden_vector():
    from backend.harness_diagnostics import diagnostic_identity
    assert diagnostic_identity("p1", "run-1", "00000000-0000-0000-0000-000000000001") == "47ea8aa7-e7e3-5576-b5c9-7e63cd8d40b7"


def test_receipt_lost_ack_reopen_conflict_and_terminal(tmp_path):
    transport = RunnerTransport(tmp_path / "transport.db")
    transport.message("p1", "run-1", "client", "go", backend_run_id="backend-1")
    source = usage()
    metadata = frame(source)
    assert transport.diagnostic("p1", metadata) is None
    transport.ingest("p1", 2, source)
    transport.ingest("p1", 3, event("run.failed", {"code": "result_error", "retryable": True}))
    public_before = transport.events("p1", "run-1")
    cursor_before = transport.hello("p1")
    receipt = transport.diagnostic("p1", metadata)
    assert receipt["status"] == "accepted"
    reopened = RunnerTransport(transport.database)
    assert reopened.diagnostic("p1", metadata) == receipt
    assert len(reopened.diagnostic_snapshots("backend-1")) == 1
    assert transport.events("p1", "run-1") == public_before
    assert transport.hello("p1") == cursor_before
    with pytest.raises(TransportError, match="diagnostic_conflict"):
        transport.diagnostic("p1", frame(source, value="0.2"))
    with pytest.raises(TransportError):
        transport.diagnostic("p2", metadata)
    assert not transport.ingest("p1", 2, source)  # old sender replays original body


def test_harness_answer_enqueue_acceptance_gap_unknown(tmp_path):
    transport = RunnerTransport(tmp_path / "transport.db")
    transport.message("p1", "run-1", "client", "go", backend_run_id="backend-1")
    first = usage()
    transport.ingest("p1", 2, first)
    q = {"question_id": "q1", "questions": [{"text": "Q?", "header": None,
        "options": [], "multi_select": False, "allow_custom": True}]}
    transport.ingest("p1", 3, event("facilitator.question", q))
    transport.answer("p1", "run-1", "answer-1", "q1", {"Q?": "A"}, backend_run_id="backend-2")
    gap = usage()
    transport.ingest("p1", 4, gap)
    transport.ingest("p1", 5, event("user.answer.accepted", {"question_id": "q1", "answers": {"Q?": "A"}}))
    second = usage()
    transport.ingest("p1", 6, second)
    assert transport.diagnostic("p1", frame(gap, 4))["status"] == "rejected"
    assert transport.diagnostic("p1", frame(first))
    assert transport.diagnostic("p1", frame(second, 6))
    assert len(transport.diagnostic_snapshots("backend-1")) == 1
    assert len(transport.diagnostic_snapshots("backend-2")) == 1


def test_prefeature_source_cannot_gain_guessed_binding(tmp_path):
    transport = RunnerTransport(tmp_path / "transport.db")
    transport.message("p1", "run-1", "client", "go")
    source = usage()
    transport.ingest("p1", 2, source)
    receipt = transport.diagnostic("p1", frame(source))
    assert receipt["status"] == "rejected"
    reopened = RunnerTransport(transport.database)
    assert reopened.diagnostic("p1", frame(source)) == receipt
    assert reopened.diagnostic_snapshots("backend-1") == []
    with pytest.raises(TransportError, match="diagnostic_conflict"):
        reopened.diagnostic("p1", frame(source, value="0.2"))


def test_historical_accepted_source_without_observation_is_rejected(tmp_path):
    transport = RunnerTransport(tmp_path / "transport.db")
    transport.message("p1", "run-1", "client", "go")
    source = usage()
    transport.ingest("p1", 2, source)
    # Simulate source accepted by the pre-diagnostic receiver; duplicate ingest cannot bind it.
    with transport._db() as db:
        db.execute("DELETE FROM diagnostic_sources")
    assert not transport.ingest("p1", 2, source)
    assert transport.diagnostic("p1", frame(source))["status"] == "rejected"


def test_private_ack_does_not_hide_remaining_same_row_drafts(tmp_path):
    transport = RunnerTransport(tmp_path / "transport.db")
    transport.message("p1", "run-1", "client", "go", backend_run_id="backend-1")
    source = usage()
    transport.ingest("p1", 2, source)
    assert transport.diagnostic("p1", frame(source))
    # A public sibling from the same journal row was not yet delivered at disconnect.
    reopened = RunnerTransport(transport.database)
    sibling = event("message.created", {"role_id": None, "text": "safe"})
    assert reopened.ingest("p1", 2, sibling)
    assert not reopened.ingest("p1", 2, source)
    assert len(reopened.diagnostic_snapshots("backend-1")) == 1


def test_late_unaccepted_result_never_acquires_a_receipt(tmp_path):
    transport = RunnerTransport(tmp_path / "transport.db")
    transport.message("p1", "run-1", "client", "go", backend_run_id="backend-1")
    transport.ingest("p1", 2, event("run.failed", {"code": "result_error", "retryable": True}))
    source = usage()
    with pytest.raises(TransportError, match="terminal_run"):
        transport.ingest("p1", 3, source)
    assert transport.diagnostic("p1", frame(source, 3))["status"] == "rejected"
    assert transport.diagnostic_snapshots("backend-1") == []


@pytest.mark.parametrize("mutate", [
    lambda f: f["payload"].update(value_usd=True),
    lambda f: f["payload"].update(value_usd="NaN"),
    lambda f: f["payload"].update(value_usd="-1"),
    lambda f: f["payload"].update(value_usd="01.0"),
    lambda f: f["payload"].update(secret="PRIVATE"),
    lambda f: f.update(diagnostic_id=str(uuid4())),
    lambda f: f.update(source_journal_seq=True),
])
def test_private_boundary_rejects_invalid_fields(tmp_path, mutate):
    transport = RunnerTransport(tmp_path / "transport.db")
    source = usage()
    raw = frame(source)
    mutate(raw)
    with pytest.raises(TransportError):
        transport.diagnostic("p1", raw)
