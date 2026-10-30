"""`usage.reported`: только факты из `result` SDK; неизвестное — null, а не 0."""

import json

import pytest

from ctrunner.protocol import JsonValue
from ctrunner.v0 import Draft, EventType, parse_row, translate

PROJECT = "demo-curriculum"


def rows(*raw: dict[str, JsonValue]) -> list[Draft]:
    return translate(PROJECT, [parse_row(r) for r in raw])


def row(seq: int, kind: str, payload: JsonValue, turn: str | None = "t1") -> dict[str, JsonValue]:
    return {"seq": seq, "ts": 1791273600.0 + seq, "turn_id": turn, "kind": kind, "payload": payload}


def started() -> dict[str, JsonValue]:
    return row(1, "turn_started", {"prompt": "постановка"})


def result_row(seq: int, **fields: JsonValue) -> dict[str, JsonValue]:
    return row(seq, "sdk", {"type": "result", "subtype": "success", "is_error": False, **fields})


def types(drafts: list[Draft]) -> list[str]:
    return [d.type.value for d in drafts]


USAGE_NULL: dict[str, JsonValue] = {
    "input_tokens": None, "output_tokens": None, "cache_creation_input_tokens": None,
    "cache_read_input_tokens": None, "num_turns": None, "duration_ms": None,
    "duration_api_ms": None, "cost_usd": None,
}  # fmt: skip


def test_usage_reported_carries_result_facts_before_terminal_event() -> None:
    result = result_row(
        2,
        usage={"input_tokens": 120, "output_tokens": 45, "cache_read_input_tokens": 900,
               "cache_creation_input_tokens": 0, "service_tier": "standard"},
        num_turns=3, duration_ms=4200, duration_api_ms=3900, total_cost_usd=0.41,
    )  # fmt: skip
    drafts = rows(
        started(), result, row(3, "checkpoint", {"sha": "abc"}), row(4, "turn_completed", {})
    )
    assert types(drafts) == ["run.started", "usage.reported", "run.completed"]
    assert drafts[1].payload == {
        "input_tokens": 120, "output_tokens": 45, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 900, "num_turns": 3, "duration_ms": 4200,
        "duration_api_ms": 3900, "cost_usd": 0.41,
    }  # fmt: skip


def test_unknown_usage_is_null_never_zero() -> None:
    drafts = rows(started(), result_row(2), row(3, "turn_completed", {}))
    assert drafts[1].type is EventType.USAGE_REPORTED
    assert drafts[1].payload == USAGE_NULL


def test_usage_only_reports_what_sdk_gave() -> None:
    drafts = rows(started(), result_row(2, usage={"output_tokens": 7}, total_cost_usd=0))
    assert drafts[1].payload == {**USAGE_NULL, "output_tokens": 7, "cost_usd": 0}


@pytest.mark.parametrize("bad", [True, -1, "12", None, 1.5, [1], {"n": 1}])
def test_unusable_count_becomes_null(bad: JsonValue) -> None:
    drafts = rows(started(), result_row(2, usage={"input_tokens": bad}, num_turns=bad))
    assert drafts[1].payload["input_tokens"] is None
    assert drafts[1].payload["num_turns"] is None


@pytest.mark.parametrize("bad", [True, -0.5, "0.4", None, [1]])
def test_unusable_cost_becomes_null(bad: JsonValue) -> None:
    drafts = rows(started(), result_row(2, total_cost_usd=bad))
    assert drafts[1].payload["cost_usd"] is None


def test_non_object_usage_is_ignored() -> None:
    drafts = rows(started(), result_row(2, usage="много"))
    assert drafts[1].payload == USAGE_NULL


def test_usage_precedes_run_failed_after_error_result() -> None:
    drafts = rows(
        started(),
        result_row(2, is_error=True, usage={"input_tokens": 5}),
        row(3, "turn_failed", {"reason": "result_error"}),
    )
    assert types(drafts) == ["run.started", "usage.reported", "run.failed"]


def test_late_result_after_terminal_event_is_not_published() -> None:
    drafts = rows(started(), row(2, "turn_completed", {}), result_row(3, num_turns=1))
    assert types(drafts) == ["run.started", "run.completed"]


def test_internal_result_fields_stay_out_of_usage() -> None:
    result = result_row(2, result="секрет", session_id="749df2fe", total_cost_usd=0.1)
    public = json.dumps(rows(started(), result)[1].payload)
    assert "секрет" not in public
    assert "749df2fe" not in public
    assert "total_cost_usd" not in public
