import json
from pathlib import Path

import pytest

from ctrunner.eventlog import EventLog
from ctrunner.gateway import (
    JournalFeed,
    accept_command,
    command_ack,
    event_message,
    heartbeat_message,
    hello_message,
    parse_welcome,
)
from ctrunner.inbox import Inbox
from ctrunner.protocol import EventKind, JsonValue, MessageCmd, ProtocolError, StopCmd
from ctrunner.redact import Redactor
from ctrunner.v0 import Translator

ROOT = "/workspace/project"


def open_log(path: Path) -> EventLog:
    return EventLog.open(path, Redactor.of([]), lambda: 1791273600.0)


def feed(path: Path, after: int = 0) -> JournalFeed:
    return JournalFeed(path, Translator("demo-a", ROOT), after)


def test_hello_shape() -> None:
    msg = hello_message(project_id="demo-a", runner_version="0.1.0", session_id=None, last_seq=41)
    assert msg == {"v": 1, "type": "hello", "project_id": "demo-a", "runner_version": "0.1.0",
                   "session_id": None, "last_seq": 41}  # fmt: skip


def test_parse_welcome() -> None:
    assert parse_welcome({"v": 1, "type": "welcome", "acked_seq": 37}) == 37
    bad_frames: list[object] = [
        {"type": "hello"},
        {"type": "welcome"},
        {"type": "welcome", "acked_seq": -1},
        {"type": "welcome", "acked_seq": True},
        [],
    ]
    for bad in bad_frames:
        with pytest.raises(ProtocolError):
            parse_welcome(bad)


def test_heartbeat_from_health_file_content() -> None:
    health = {
        "phase": "awaiting_answer",
        "health": "ok",
        "last_progress_at": 5.0,
        "written_at": 6.0,
    }
    assert heartbeat_message(health) == {
        "v": 1, "type": "heartbeat", "phase": "awaiting_answer", "health": "ok",
        "last_progress_at": 5.0, "turn_id": None,
    }  # fmt: skip
    assert heartbeat_message({"phase": "idle"}) is None


def test_feed_translates_journal_to_wire_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with open_log(path) as log:
        log.append(EventKind.SESSION_STARTED, None, {})
        log.append(EventKind.TURN_STARTED, "run-1", {"prompt": "go"})
        questions: list[JsonValue] = [{"question": "Q?", "options": [{"label": "A"}]}]
        log.append(EventKind.FORK_QUESTION, "run-1", {"fork_id": "f1", "questions": questions})
    polled = feed(path).poll()
    assert [(seq, [d.type.value for d in drafts]) for seq, drafts in polled] == [
        (2, ["run.started"]), (3, ["facilitator.question"]),
    ]  # fmt: skip
    wire = event_message(polled[1][0], polled[1][1][0])
    assert wire["type"] == "event"
    assert wire["journal_seq"] == 3
    body = wire["event"]
    assert isinstance(body, dict)
    assert body["run_id"] == "run-1"
    assert body["type"] == "facilitator.question"
    assert body["occurred_at"].endswith("Z")  # type: ignore[union-attr]
    assert set(body) == {"event_id", "project_id", "run_id", "occurred_at", "type", "payload"}


def test_feed_skips_rows_before_acked_seq_but_still_folds_them(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with open_log(path) as log:
        log.append(EventKind.TURN_STARTED, "run-1", {"prompt": "go"})
        log.append(EventKind.FORK_QUESTION, "run-1", {"fork_id": "f1", "questions": []})
        log.append(EventKind.FORK_ANSWERED, "run-1", {"fork_id": "f1", "answers": {}})
    assert [seq for seq, _ in feed(path, after=2).poll()] == [2, 3]


def test_feed_resends_acked_row_because_it_may_be_half_written(tmp_path: Path) -> None:
    """`acked_seq` = номер строки, дошедшей хотя бы одним событием; остальные события строки
    досылаются, а повторные отсекает gateway по `event_id`."""
    path = tmp_path / "events.jsonl"
    with open_log(path) as log:
        log.append(EventKind.TURN_STARTED, "run-1", {"prompt": "go"})
    assert [seq for seq, _ in feed(path, after=1).poll()] == [1]


def test_feed_is_incremental_and_waits_for_whole_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    f = feed(path)
    assert f.poll() == []  # файла ещё нет
    with open_log(path) as log:
        log.append(EventKind.TURN_STARTED, "run-1", {"prompt": "go"})
    assert [seq for seq, _ in f.poll()] == [1]
    assert f.poll() == []
    with path.open("a", encoding="utf-8") as fh:  # недописанная строка не читается
        fh.write('{"seq": 2, "ts": 1.0, "turn_id": "run-1", "kind": "turn_comp')
    assert f.poll() == []
    with path.open("a", encoding="utf-8") as fh:
        fh.write('leted", "payload": {}}\n')
    assert [seq for seq, _ in f.poll()] == [2]


def test_feed_skips_poison_row(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"seq": 1, "kind": "bogus"}\n', encoding="utf-8")
    f = feed(path)
    assert f.poll() == []
    with open_log(path) as log:
        log.append(EventKind.TURN_STARTED, "run-1", {"prompt": "go"})
    assert [seq for seq, _ in f.poll()] == [2]


def test_accept_message_writes_inbox_and_acks(tmp_path: Path) -> None:
    ack = accept_command(
        {"v": 1, "type": "command", "id": "run-9", "kind": "message", "text": "hi", "junk": 1},
        inbox_dir=tmp_path, fork_questions=lambda _: None,
    )  # fmt: skip
    assert ack == command_ack("run-9", "accepted")
    assert [d.command for d in Inbox(tmp_path).peek()] == [MessageCmd("run-9", "hi")]


def test_accept_stop(tmp_path: Path) -> None:
    ack = accept_command({"type": "command", "id": "s1", "kind": "stop"},
                         inbox_dir=tmp_path, fork_questions=lambda _: None)  # fmt: skip
    assert ack["status"] == "accepted"
    assert [d.command for d in Inbox(tmp_path).peek()] == [StopCmd("s1")]


def test_reject_malformed_command_without_side_effects(tmp_path: Path) -> None:
    ack = accept_command({"type": "command", "id": "x", "kind": "bogus"},
                         inbox_dir=tmp_path, fork_questions=lambda _: None)  # fmt: skip
    assert ack["status"] == "rejected"
    assert ack["id"] == "x"
    assert list(tmp_path.glob("*.json")) == []


def test_fork_answer_for_unknown_fork_is_rejected(tmp_path: Path) -> None:
    raw = {"type": "command", "id": "a1", "kind": "fork_answer", "fork_id": "f9",
           "answers": {"Q?": "A"}}  # fmt: skip
    ack = accept_command(raw, inbox_dir=tmp_path, fork_questions=lambda _: None)
    assert ack == command_ack("a1", "rejected", "unknown_fork")
    assert list(tmp_path.glob("*.json")) == []


def test_fork_answer_with_foreign_keys_is_rejected(tmp_path: Path) -> None:
    raw = {"type": "command", "id": "a1", "kind": "fork_answer", "fork_id": "f1",
           "answers": {"Другой вопрос?": "A"}}  # fmt: skip
    ack = accept_command(raw, inbox_dir=tmp_path, fork_questions=lambda _: ("Q?",))
    assert ack == command_ack("a1", "rejected", "answers_mismatch")


def test_fork_answer_for_open_fork_is_accepted(tmp_path: Path) -> None:
    raw = {"type": "command", "id": "a1", "kind": "fork_answer", "fork_id": "f1",
           "answers": {"Q?": "A"}}  # fmt: skip
    ack = accept_command(raw, inbox_dir=tmp_path, fork_questions=lambda _: ("Q?",))
    assert ack == command_ack("a1", "accepted")
    assert json.loads(next(tmp_path.glob("*.json")).read_text())["fork_id"] == "f1"


def test_retried_fork_answer_after_lost_ack_is_accepted_again(tmp_path: Path) -> None:
    """Gateway повторяет команду с тем же `id`; развилка к тому времени уже закрыта ответом."""
    raw = {"type": "command", "id": "a1", "kind": "fork_answer", "fork_id": "f1",
           "answers": {"Q?": "A"}}  # fmt: skip
    first = accept_command(raw, inbox_dir=tmp_path, fork_questions=lambda _: ("Q?",))
    retry = accept_command(raw, inbox_dir=tmp_path, fork_questions=lambda _: None)
    assert first == retry == command_ack("a1", "accepted")
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_retried_command_already_processed_is_accepted_again(tmp_path: Path) -> None:
    raw = {"type": "command", "id": "a1", "kind": "fork_answer", "fork_id": "f1",
           "answers": {"Q?": "A"}}  # fmt: skip
    accept_command(raw, inbox_dir=tmp_path, fork_questions=lambda _: ("Q?",))
    inbox = Inbox(tmp_path)
    inbox.ack(inbox.peek()[0])
    retry = accept_command(raw, inbox_dir=tmp_path, fork_questions=lambda _: None)
    assert retry == command_ack("a1", "accepted")
    assert list(tmp_path.glob("*.json")) == []
