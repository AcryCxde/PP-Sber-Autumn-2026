"""Private metadata never changes the legacy translator's bytes or identity."""

import json
from pathlib import Path

import pytest

from ctrunner.gateway import JournalFeed, event_message
from ctrunner.harness_diagnostic import DiagnosticFeed
from ctrunner.v0 import Translator


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


def test_decimal_canonical_and_missing_session(tmp_path):
    path = tmp_path / "events.jsonl"
    journal(path, "1.2300e-2")
    assert (
        DiagnosticFeed(path, "p1", "/workspace/project").poll()[0]["payload"]["value_usd"]
        == "0.0123"
    )
    journal(path, session="")
    assert DiagnosticFeed(path, "p1", "/workspace/project").poll() == []
