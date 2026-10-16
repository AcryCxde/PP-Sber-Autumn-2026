import json
from pathlib import Path

import pytest

from ctrunner.inbox import Inbox, answers_for, put
from ctrunner.protocol import Command, MessageCmd


def drain(box: Inbox) -> list[Command]:
    deliveries = box.peek()
    for delivery in deliveries:
        box.ack(delivery)
    return [d.command for d in deliveries]


def test_put_then_take(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "go"})
    box = Inbox(tmp_path)
    assert drain(box) == [MessageCmd("c1", "go")]
    assert drain(box) == []  # файл перенесён в processed/


def test_take_preserves_put_order(tmp_path: Path) -> None:
    for i in range(3):
        put(tmp_path, {"id": f"c{i}", "kind": "message", "text": str(i)})
    assert [c.id for c in drain(Inbox(tmp_path))] == ["c0", "c1", "c2"]


def test_malformed_goes_to_rejected(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "bogus"})
    (tmp_path / "broken.json").write_text("{")
    assert drain(Inbox(tmp_path)) == []
    assert len(list((tmp_path / "rejected").iterdir())) == 2


def test_duplicate_id_ignored(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    box = Inbox(tmp_path)
    drain(box)
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    assert drain(box) == []


def test_duplicate_survives_restart(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    drain(Inbox(tmp_path))
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    assert drain(Inbox(tmp_path)) == []


def test_answers_for_maps_labels_to_questions_from_log(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    question = {"fork_id": "f1", "questions": [{"question": "Q1?"}, {"question": "Q2?"}]}
    rows = [
        {"seq": 1, "kind": "sdk", "payload": {}},
        {"seq": 2, "kind": "fork_question", "payload": question},
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert answers_for(log, "f1", ["A", "B"]) == {"Q1?": "A", "Q2?": "B"}
    with pytest.raises(ValueError, match="2 answers"):
        answers_for(log, "f1", ["A"])
    with pytest.raises(ValueError, match="unknown fork"):
        answers_for(log, "nope", ["A"])


def test_skipped_commands_are_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    put(tmp_path, {"id": "c2", "kind": "bogus"})
    drain(Inbox(tmp_path))
    events = [json.loads(line)["event"] for line in capsys.readouterr().err.splitlines()]
    assert events == ["command_duplicate", "command_rejected"]


def test_peek_without_ack_redelivers(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "go"})
    box = Inbox(tmp_path)
    first = box.peek()
    assert [d.command for d in first] == [MessageCmd("c1", "go")]
    assert [d.command for d in box.peek()] == [MessageCmd("c1", "go")]  # файл ещё на месте
    box.ack(first[0])
    assert box.peek() == []
    assert len(list((tmp_path / "processed").iterdir())) == 1


def test_unacked_command_survives_restart(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "go"})
    Inbox(tmp_path).peek()  # процесс упал до ack
    assert [d.command for d in Inbox(tmp_path).peek()] == [MessageCmd("c1", "go")]


def test_same_id_twice_in_one_batch_is_delivered_once(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    assert len(Inbox(tmp_path).peek()) == 1
