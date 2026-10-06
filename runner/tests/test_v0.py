import json
from pathlib import Path

import pytest

from ctrunner.protocol import JsonValue, ProtocolError
from ctrunner.v0 import (
    TERMINAL,
    Draft,
    EventType,
    Sequencer,
    parse_row,
    read_journal,
    translate,
)

JOURNALS = Path(__file__).parent / "journals"
FIXTURES = Path(__file__).parents[2] / "docs" / "contracts" / "fixtures"
PROJECT = "demo-curriculum"
GATEWAY_TYPES = {EventType.RUN_ACCEPTED, EventType.ARTIFACT_SAVED, EventType.RUN_OUTCOME_UNKNOWN}


def journal(name: str) -> list[Draft]:
    lines = (JOURNALS / f"{name}.events.jsonl").read_text().splitlines()
    return translate(PROJECT, read_journal(lines))


def rows(*raw: dict[str, JsonValue]) -> list[Draft]:
    return translate(PROJECT, [parse_row(r) for r in raw])


def row(seq: int, kind: str, payload: JsonValue, turn: str | None = "t1") -> dict[str, JsonValue]:
    return {"seq": seq, "ts": 1791273600.0 + seq, "turn_id": turn, "kind": kind, "payload": payload}


def started(seq: int = 1, turn: str = "t1") -> dict[str, JsonValue]:
    return row(seq, "turn_started", {"prompt": "секретная постановка"}, turn)


def assistant(
    seq: int, content: list[JsonValue], parent: str | None = None
) -> dict[str, JsonValue]:
    msg: dict[str, JsonValue] = {"model": "m", "content": content}
    return row(seq, "sdk", {"type": "assistant", "parent_tool_use_id": parent, "message": msg})


def tool_result(seq: int, tool_id: str, *, error: bool = False) -> dict[str, JsonValue]:
    block: dict[str, JsonValue] = {"tool_use_id": tool_id, "content": "ok", "is_error": error}
    msg: dict[str, JsonValue] = {"content": [block]}
    return row(seq, "sdk", {"type": "user", "parent_tool_use_id": None, "message": msg})


def types(drafts: list[Draft]) -> list[str]:
    return [d.type.value for d in drafts]


def test_happy_journal_maps_to_public_lifecycle() -> None:
    assert types(journal("happy")) == [
        "run.started",
        "message.created",
        "facilitator.question",
        "user.answer.accepted",
        "role.started",
        "role.started",
        "message.created",
        "role.completed",
        "message.created",
        "role.completed",
        "message.created",
        "artifact.created",
        "run.completed",
    ]


def test_internal_data_never_reaches_public_feed() -> None:
    public = json.dumps([d.payload for d in journal("happy")], ensure_ascii=False)
    hidden = ["Пересмотри учебную программу", "нужны методист", "/workspace", "toolu_",
              "749df2fe", "total_cost_usd", "notes.md", "broken.md", "state.json"]  # fmt: skip
    assert [h for h in hidden if h in public] == []


def test_role_messages_are_attributed_to_the_role() -> None:
    messages = [d for d in journal("happy") if d.type is EventType.MESSAGE_CREATED]
    authors = [d.payload["role_id"] for d in messages]
    assert authors == [None, "methodologist", "data-science-expert", None]


def test_retranslation_gives_same_event_ids() -> None:
    assert [d.event_id for d in journal("happy")] == [d.event_id for d in journal("happy")]


def test_sequencer_numbers_per_run_and_drops_duplicates() -> None:
    first, second = journal("happy"), journal("failed")
    seq = Sequencer()
    events = seq.assign([*first, *second[:3]]) + seq.assign([*first, *second])
    by_run: dict[str, list[int]] = {}
    for e in events:
        by_run.setdefault(e.draft.run_id, []).append(e.seq)
    assert [list(range(1, len(s) + 1)) for s in by_run.values()] == list(by_run.values())
    assert len(events) == len(first) + len(second)


def test_process_crash_is_not_terminal_and_run_continues() -> None:
    drafts = journal("failed")
    assert types(drafts)[2:] == [
        "progress.updated",
        "progress.updated",
        "progress.updated",
        "message.created",
        "run.failed",
    ]
    assert [d.payload.get("stage") for d in drafts[2:5]] == ["interrupted", "recovering", "working"]
    assert drafts[-1].payload == {"code": "result_error", "retryable": True}


def test_legacy_sdk_crashed_without_fatal_flag_is_not_terminal() -> None:
    drafts = rows(started(), row(2, "turn_failed", {"reason": "sdk_crashed"}))
    assert types(drafts) == ["run.started", "progress.updated"]


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [("result_error", True), ("checkpoint_failed", True), ("resume_limit", True),
     ("disk_full", False)],
)  # fmt: skip
def test_non_fatal_failure_is_terminal(reason: str, retryable: bool) -> None:
    failed = row(2, "turn_failed", {"reason": reason})
    drafts = rows(started(), failed, assistant(3, [{"text": "x"}]))
    assert types(drafts) == ["run.started", "run.failed"]
    assert drafts[-1].payload == {"code": reason, "retryable": retryable}


def test_state_corrupt_marks_only_open_runs_unknown() -> None:
    drafts = rows(
        started(1, "t1"),
        row(2, "turn_completed", {}, "t1"),
        started(3, "t2"),
        row(4, "turn_failed", {"reason": "state_corrupt"}, None),
    )
    assert [(d.run_id, d.type.value) for d in drafts] == [
        ("t1", "run.started"),
        ("t1", "run.completed"),
        ("t2", "run.started"),
        ("t2", "run.outcome_unknown"),
    ]


def test_only_successful_writes_inside_project_become_artifacts() -> None:
    writes: list[JsonValue] = [
        {"id": "w1", "name": "Write", "input": {"file_path": "/workspace/project/a.md"}},
        {"id": "w2", "name": "Edit", "input": {"file_path": "docs/b.md"}},
        {"id": "w3", "name": "Write", "input": {"file_path": "/workspace/project/../x.md"}},
        {"id": "w4", "name": "Write", "input": {"file_path": "/workspace/project/.git/config"}},
        {"id": "w5", "name": "Write", "input": {"file_path": "/workspace/project/c.md"}},
        {"id": "w6", "name": "Write", "input": {"file_path": "/workspace/project/d.md"}},
    ]
    drafts = rows(
        started(),
        assistant(2, writes),
        *[tool_result(3 + i, f"w{i + 1}", error=i == 4) for i in range(5)],  # w6 без ответа
        row(9, "checkpoint", {"sha": "abc"}),
    )
    paths = [d.payload["path"] for d in drafts if d.type is EventType.ARTIFACT_CREATED]
    assert paths == ["a.md", "docs/b.md"]


def test_malformed_question_fields_are_dropped() -> None:
    questions: JsonValue = [
        {"question": "Q1", "options": [{"label": "A"}, "мусор"], "multiSelect": True},
        {"options": []},
        "мусор",
    ]
    drafts = rows(started(), row(2, "fork_question", {"fork_id": "f1", "questions": questions}))
    assert drafts[-1].payload == {
        "question_id": "f1",
        "questions": [{
            "text": "Q1", "header": None, "options": [{"label": "A", "description": None}],
            "multi_select": True, "allow_custom": True,
        }],
    }  # fmt: skip


def test_events_after_terminal_are_not_published() -> None:
    drafts = rows(
        started(),
        row(2, "turn_completed", {}),
        row(3, "turn_interrupted", {}),
        assistant(4, [{"text": "поздно"}]),
    )
    assert types(drafts) == ["run.started", "run.completed"]


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"seq": 0, "ts": 1.0, "turn_id": None, "kind": "sdk"},
        {"seq": True, "ts": 1.0, "turn_id": None, "kind": "sdk"},
        {"seq": 1, "ts": "1", "turn_id": None, "kind": "sdk"},
        {"seq": 1, "ts": 1.0, "turn_id": 5, "kind": "sdk"},
        {"seq": 1, "ts": 1.0, "turn_id": None, "kind": "bogus"},
    ],
)
def test_parse_row_rejects_malformed_rows(raw: object) -> None:
    with pytest.raises(ProtocolError):
        parse_row(raw)


def test_unknown_failure_reason_is_a_protocol_error() -> None:
    with pytest.raises(ProtocolError):
        rows(started(), row(2, "turn_failed", {"reason": "bogus"}))


# --- fixture для 2.1/2.5: публичная лента после gateway -------------------------------------


def fixture(name: str) -> list[dict[str, JsonValue]]:
    return [json.loads(x) for x in (FIXTURES / f"{name}.v0.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("name", ["happy", "failed", "unknown"])
def test_fixture_obeys_contract(name: str) -> None:
    feed = fixture(name)
    envelope = {"schema", "event_id", "project_id", "run_id", "seq", "occurred_at", "type",
                "payload"}  # fmt: skip
    assert all(set(e) == envelope and e["schema"] == "events.v0" for e in feed)
    assert [e["seq"] for e in feed] == list(range(1, len(feed) + 1))
    assert len({e["event_id"] for e in feed}) == len(feed)
    assert feed[0]["type"] == EventType.RUN_ACCEPTED
    terminals = [i for i, e in enumerate(feed) if e["type"] in TERMINAL]
    assert terminals == [len(feed) - 1]
    created = [e["payload"]["artifact_id"] for e in feed if e["type"] == "artifact.created"]  # type: ignore[index,call-overload]
    saved = [e["payload"]["artifact_id"] for e in feed if e["type"] == "artifact.saved"]  # type: ignore[index,call-overload]
    if feed[-1]["type"] == EventType.RUN_COMPLETED:
        assert created == saved


@pytest.mark.parametrize("name", ["happy", "failed", "unknown"])
def test_fixture_runner_part_matches_adapter(name: str) -> None:
    runner_part = [
        (e["event_id"], e["type"], e["payload"])
        for e in fixture(name)
        if e["type"] not in GATEWAY_TYPES or e["event_id"] in {d.event_id for d in journal(name)}
    ]
    assert runner_part == [(d.event_id, d.type.value, d.payload) for d in journal(name)]
