"""Private metadata never changes the legacy translator's bytes or identity."""

import json
from pathlib import Path

import pytest

import ctrunner.harness_diagnostic as diagnostic_module
from ctrunner.gateway import JournalFeed, event_message
from ctrunner.harness_diagnostic import DiagnosticFeed, DiagnosticReceipts
from ctrunner.v0 import EventType, Translator, parse_row


def journal(path: Path, value: str = "0.062745", session: str = "private-session"):
    path.write_text(
        '{"seq":1,"ts":1791273601,"turn_id":"run-1","kind":"turn_started","payload":{"prompt":"SECRET"}}\n'
        '{"seq":2,"ts":1791273602,"turn_id":"run-1","kind":"sdk","payload":{"type":"result","session_id":'
        + json.dumps(session)
        + ',"total_cost_usd":'
        + value
        + "}}\n"
    )


def test_private_pass_reads_below_cursor_without_changing_legacy(tmp_path):
    path = tmp_path / "events.jsonl"
    journal(path)
    before = JournalFeed(path, Translator("p1", "/workspace/project"), 0).poll()
    feed = DiagnosticFeed(path, "p1", "/workspace/project")
    frames = feed.poll()
    assert len(frames) == 1
    frame = frames[0]
    assert frame["payload"]["value_usd"] == "0.062745"
    assert set(frame["payload"]) == {"session_ref", "observed_at", "value_usd"}
    assert "SECRET" not in json.dumps(frame) and "private-session" not in json.dumps(frame)
    legacy = [event_message(seq, draft) for seq, ds in before for draft in ds]
    usage = next(e for e in legacy if e["event"]["type"] == "usage.reported")
    assert frame["source_event_id"] == usage["event"]["event_id"]
    assert frame["source_journal_seq"] == 2
    after = JournalFeed(path, Translator("p1", "/workspace/project"), 0).poll()
    assert before == after
    assert DiagnosticFeed(path, "p1", "/workspace/project").poll() == frames
    assert feed.poll() == []


@pytest.mark.parametrize("value", ["true", "-1", "NaN", "1e100"])
def test_invalid_amount_is_not_a_zero_snapshot(tmp_path, value):
    path = tmp_path / "events.jsonl"
    journal(path, value)
    assert DiagnosticFeed(path, "p1", "/workspace/project").poll() == []


def test_feed_pages_and_definitive_receipts_survive_reopen(tmp_path):
    path = tmp_path / "events.jsonl"
    journal(path)
    feed = DiagnosticFeed(path, "p1", "/workspace/project")
    assert feed.poll(0) == []
    assert feed.poll(1) == []  # bounded scan of the non-result row
    frames = feed.poll(1)
    assert len(frames) == 1
    receipts = DiagnosticReceipts(path)
    receipts.record(frames[0]["diagnostic_id"], "rejected")
    reopened = DiagnosticReceipts(path)
    assert reopened.contains(frames[0]["diagnostic_id"])
    with pytest.raises(ValueError, match="invalid_diagnostic_receipt"):
        reopened.record("unknown", "transient")


def test_feed_rewind_retries_without_inventing_a_receipt(tmp_path):
    path = tmp_path / "events.jsonl"
    journal(path)
    feed = DiagnosticFeed(path, "p1", "/workspace/project")
    original = feed.poll()
    assert feed.caught_up()
    feed.rewind()
    assert not feed.caught_up()
    assert feed.poll() == original
    assert not DiagnosticReceipts(path).contains(original[0]["diagnostic_id"])


def test_diagnostic_fold_does_not_retain_role_or_file_state(tmp_path):
    path = tmp_path / "events.jsonl"
    rows = [
        {"seq": 1, "ts": 1, "turn_id": "r", "kind": "turn_started", "payload": {}},
        {"seq": 2, "ts": 2, "turn_id": "r", "kind": "sdk", "payload": {
            "type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Agent", "id": "agent", "input": {}},
                {"type": "tool_use", "name": "Write", "id": "write", "input": {
                    "file_path": "/workspace/project/private.txt"}},
            ]}}},
    ]
    for seq in range(3, 1003):
        rows.append({**rows[1], "seq": seq})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    feed = DiagnosticFeed(path, "p", "/workspace/project")
    while not feed.caught_up():
        assert feed.poll() == []
    assert not hasattr(feed, "_translator")
    assert feed._live == {"r"}
    assert feed._closed == set()


def test_feed_only_decimal_parses_live_results(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    journal(path, "0.12345678901234567890123456789")
    original = diagnostic_module.json.loads
    exact_calls = []

    def counted_loads(raw, **kwargs):
        if "parse_float" in kwargs:
            exact_calls.append(raw)
        return original(raw, **kwargs)

    monkeypatch.setattr(diagnostic_module.json, "loads", counted_loads)
    frames = DiagnosticFeed(path, "p1", "/workspace/project").poll()
    assert len(exact_calls) == 1
    assert frames[0]["payload"]["value_usd"] == "0.12345678901234567890123456789"


def test_feed_reports_full_scan_page_without_frames(tmp_path):
    path = tmp_path / "events.jsonl"
    journal(path)
    feed = DiagnosticFeed(path, "p1", "/workspace/project")
    assert feed.poll(1) == []
    assert feed.scan_pending
    assert len(feed.poll(1)) == 1
    assert not feed.scan_pending
    assert feed.poll() == []
    assert not feed.scan_pending


def test_incomplete_tail_does_not_request_immediate_scan(tmp_path):
    path = tmp_path / "events.jsonl"
    journal(path)
    with path.open("ab") as stream:
        stream.write(b'{"seq":3')
    feed = DiagnosticFeed(path, "p1", "/workspace/project")
    assert len(feed.poll()) == 1
    assert not feed.scan_pending
    assert not feed.caught_up()
    assert feed.poll() == []
    assert not feed.scan_pending


def test_terminal_result_is_not_decimal_parsed(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    journal(path)
    with path.open("a") as stream:
        stream.write('{"seq":3,"ts":3,"turn_id":"run-1","kind":"turn_completed",'
                     '"payload":{}}\n')
        stream.write('{"seq":4,"ts":4,"turn_id":"run-1","kind":"sdk",'
                     '"payload":{"type":"result","session_id":"s","total_cost_usd":2}}\n')
    original = diagnostic_module.json.loads
    exact_calls = []

    def counted_loads(raw, **kwargs):
        if "parse_float" in kwargs:
            exact_calls.append(raw)
        return original(raw, **kwargs)

    monkeypatch.setattr(diagnostic_module.json, "loads", counted_loads)
    assert len(DiagnosticFeed(path, "p1", "/workspace/project").poll()) == 1
    assert len(exact_calls) == 1


def test_decimal_canonical_and_missing_session(tmp_path):
    path = tmp_path / "events.jsonl"
    journal(path, "1.2300e-2")
    assert (
        DiagnosticFeed(path, "p1", "/workspace/project").poll()[0]["payload"]["value_usd"]
        == "0.0123"
    )
    journal(path, session="")
    assert DiagnosticFeed(path, "p1", "/workspace/project").poll() == []


@pytest.mark.parametrize("failure", ["result_error", "checkpoint_failed", "resume_limit",
                                     "sdk_crashed", "state_corrupt", "disk_full"])
def test_minimal_fold_matches_public_usage_identity(tmp_path, failure):
    path = tmp_path / "events.jsonl"
    rows = []

    def add(kind, payload, run="r"):
        rows.append({"seq": len(rows) + 1, "ts": len(rows) + 1,
                     "turn_id": run, "kind": kind, "payload": payload})

    def result(**extra):
        add("sdk", {"type": "result", "session_id": "s", "total_cost_usd": 1, **extra})

    result()  # No started turn, so no public usage.
    add("turn_started", {})
    result()
    result(usage={"input_tokens": -1, "output_tokens": True})
    add("fork_question", {"fork_id": "f", "questions": []})
    add("fork_answered", {"fork_id": "f", "answers": {}})
    add("turn_failed", {"reason": failure})
    result()
    add("turn_started", {})  # Duplicate ID must not reopen a terminal turn.
    result()
    add("turn_started", {}, "other")
    add("sdk", {"type": "result", "session_id": "s", "total_cost_usd": 1}, "other")
    add("turn_completed", {}, "other")
    add("sdk", {"type": "result", "session_id": "s", "total_cost_usd": 1}, "other")
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    public = Translator("p", "/workspace/project")
    expected = [draft.event_id for row in rows for draft in public.feed(parse_row(row))
                if draft.type is EventType.USAGE_REPORTED]
    feed = DiagnosticFeed(path, "p", "/workspace/project")
    assert [frame["source_event_id"] for frame in feed.poll()] == expected
    assert not hasattr(feed, "_translator")
