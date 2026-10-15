import pytest

from ctrunner.protocol import (
    ForkAnswerCmd,
    MessageCmd,
    ProtocolError,
    StopCmd,
    parse_command,
)


def test_parse_message() -> None:
    raw = {"id": "c1", "kind": "message", "text": "привет"}
    assert parse_command(raw) == MessageCmd("c1", "привет")


def test_parse_fork_answer() -> None:
    raw = {"id": "c2", "kind": "fork_answer", "fork_id": "f1", "answers": {"Вопрос?": "Да"}}
    assert parse_command(raw) == ForkAnswerCmd("c2", "f1", {"Вопрос?": "Да"})


def test_parse_stop() -> None:
    assert parse_command({"id": "c3", "kind": "stop"}) == StopCmd("c3")


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"id": "c", "kind": "nope"},
        {"id": "", "kind": "stop"},
        {"id": 1, "kind": "stop"},
        {"id": "c", "kind": "message"},
        {"id": "c", "kind": "message", "text": ""},
        {"id": "c", "kind": "fork_answer", "fork_id": "f", "answers": {"q": 1}},
        {"id": "c", "kind": "fork_answer", "fork_id": "", "answers": {}},
        ["not", "object"],
        {"id": "a b", "kind": "stop"},
        {"id": "line\nbreak", "kind": "stop"},
        {"id": "x" * 65, "kind": "stop"},
    ],
)
def test_rejects_malformed(raw: object) -> None:
    with pytest.raises(ProtocolError):
        parse_command(raw)


def test_accepts_uuid_command_id() -> None:
    raw = {"id": "7c2e4a90-b1d3-4f6e-8a5b-0c9d1e2f3a4b", "kind": "stop"}
    assert parse_command(raw) == StopCmd("7c2e4a90-b1d3-4f6e-8a5b-0c9d1e2f3a4b")
