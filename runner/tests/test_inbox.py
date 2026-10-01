import json
from pathlib import Path

import pytest

from ctrunner.inbox import Inbox, answers_for, put
from ctrunner.protocol import MessageCmd


def test_put_then_take(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "go"})
    box = Inbox(tmp_path)
    assert box.take() == [MessageCmd("c1", "go")]
    assert box.take() == []  # файл перенесён в processed/


def test_take_preserves_put_order(tmp_path: Path) -> None:
    for i in range(3):
        put(tmp_path, {"id": f"c{i}", "kind": "message", "text": str(i)})
    assert [c.id for c in Inbox(tmp_path).take()] == ["c0", "c1", "c2"]


def test_malformed_goes_to_rejected(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "bogus"})
    (tmp_path / "broken.json").write_text("{")
    assert Inbox(tmp_path).take() == []
    assert len(list((tmp_path / "rejected").iterdir())) == 2


def test_duplicate_id_ignored(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    box = Inbox(tmp_path)
    box.take()
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    assert box.take() == []


def test_duplicate_survives_restart(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    Inbox(tmp_path).take()
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    assert Inbox(tmp_path).take() == []


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
    Inbox(tmp_path).take()
    events = [json.loads(line)["event"] for line in capsys.readouterr().err.splitlines()]
    assert events == ["command_duplicate", "command_rejected"]
