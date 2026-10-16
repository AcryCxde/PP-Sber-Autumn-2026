import json
from pathlib import Path

import pytest

from ctrunner.eventlog import EventLog, kinds_of_turn
from ctrunner.fsio import write_json_atomic
from ctrunner.protocol import EventKind
from ctrunner.redact import Redactor

R = Redactor.of(["SECRET-TOKEN-1"])


def lines(p: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in p.read_text().splitlines()]


def test_seq_is_monotonic_across_reopen(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    with EventLog.open(p, R, clock=lambda: 1.0) as log:
        log.append(EventKind.SESSION_STARTED, None, {})
    with EventLog.open(p, R, clock=lambda: 2.0) as log:
        assert log.append(EventKind.TURN_STARTED, "t1", {}).seq == 2
    assert [x["seq"] for x in lines(p)] == [1, 2]


def test_payload_is_redacted_on_disk(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    with EventLog.open(p, R, clock=lambda: 1.0) as log:
        log.append(EventKind.SDK, "t1", {"text": "key SECRET-TOKEN-1"})
    assert "SECRET-TOKEN-1" not in p.read_text()


def test_torn_last_line_is_truncated(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    whole = '{"seq": 1, "ts": 1, "turn_id": null, "kind": "sdk", "payload": {}}\n'
    p.write_text(whole + '{"seq": 2, "ki')
    with EventLog.open(p, R, clock=lambda: 1.0) as log:
        assert log.append(EventKind.SDK, None, {}).seq == 2
    assert [x["seq"] for x in lines(p)] == [1, 2]


def test_atomic_write_keeps_old_file_on_failure(tmp_path: Path) -> None:
    p = tmp_path / "h.json"
    write_json_atomic(p, {"v": 1})
    bad: dict[str, object] = {"v": object()}
    with pytest.raises(TypeError):
        write_json_atomic(p, bad)  # type: ignore[arg-type]
    assert json.loads(p.read_text()) == {"v": 1}
    assert list(tmp_path.iterdir()) == [p]


def test_kinds_of_turn_collects_only_that_turn(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    rows = [
        {"seq": 1, "turn_id": "t1", "kind": "turn_started"},
        {"seq": 2, "turn_id": "t2", "kind": "checkpoint"},
        {"seq": 3, "turn_id": "t1", "kind": "checkpoint"},
        {"seq": 4, "turn_id": None, "kind": "session_started"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert kinds_of_turn(path, "t1") == {EventKind.TURN_STARTED, EventKind.CHECKPOINT}


def test_kinds_of_turn_without_log_is_empty(tmp_path: Path) -> None:
    assert kinds_of_turn(tmp_path / "missing.jsonl", "t1") == frozenset()


def test_recovery_event_kinds_are_serialised() -> None:
    assert {EventKind.CHECKPOINT, EventKind.TURN_INTERRUPTED, EventKind.TURN_RESUMED} == {
        EventKind("checkpoint"),
        EventKind("turn_interrupted"),
        EventKind("turn_resumed"),
    }
