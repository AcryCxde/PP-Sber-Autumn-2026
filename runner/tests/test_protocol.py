import pytest

from ctrunner.protocol import (
    MAX_INPUT_FILES,
    MAX_INPUT_PATH_LEN,
    MAX_LABEL_LEN,
    MAX_ROLES,
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


def test_parse_message_with_inputs() -> None:
    raw = {
        "id": "c1", "kind": "message", "text": "go", "roles": ["методист", "рецензент"],
        "stage": "ревью", "input_files": ["inputs/a.md", "b/c.txt"],
    }  # fmt: skip
    assert parse_command(raw) == MessageCmd(
        "c1", "go", ("методист", "рецензент"), "ревью", ("inputs/a.md", "b/c.txt")
    )


def test_message_inputs_are_optional_and_null_means_absent() -> None:
    raw = {"id": "c1", "kind": "message", "text": "go", "roles": None, "stage": None,
           "input_files": None}  # fmt: skip
    assert parse_command(raw) == MessageCmd("c1", "go")


def _message(**extra: object) -> dict[str, object]:
    return {"id": "c", "kind": "message", "text": "go", **extra}


@pytest.mark.parametrize(
    "extra",
    [
        {"roles": "методист"},
        {"roles": [1]},
        {"roles": [""]},
        {"roles": ["x" * (MAX_LABEL_LEN + 1)]},
        {"roles": ["r"] * (MAX_ROLES + 1)},
        {"stage": 1},
        {"stage": ""},
        {"stage": "x" * (MAX_LABEL_LEN + 1)},
        {"input_files": "a.md"},
        {"input_files": [1]},
        {"input_files": [""]},
        {"input_files": ["/etc/passwd"]},
        {"input_files": ["/workspace/project/a.md"]},
        {"input_files": ["../a.md"]},
        {"input_files": ["a/../../b"]},
        {"input_files": ["a\\..\\b"]},
        {"input_files": ["a\x00b"]},
        {"input_files": ["a\nb"]},
        {"input_files": ["x" * (MAX_INPUT_PATH_LEN + 1)]},
        {"input_files": [f"f{i}" for i in range(MAX_INPUT_FILES + 1)]},
    ],
)
def test_rejects_bad_message_inputs(extra: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        parse_command(_message(**extra))


def test_accepts_limits_exactly() -> None:
    cmd = parse_command(_message(
        roles=["r"] * MAX_ROLES, stage="s" * MAX_LABEL_LEN,
        input_files=["p" * MAX_INPUT_PATH_LEN] + [f"f{i}" for i in range(MAX_INPUT_FILES - 1)],
    ))  # fmt: skip
    assert isinstance(cmd, MessageCmd)
    assert len(cmd.input_files) == MAX_INPUT_FILES
