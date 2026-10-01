import pytest
from claude_agent_sdk import ResultMessage, SystemMessage

from ctrunner.liveness import Phase
from ctrunner.protocol import MessageCmd
from ctrunner.turn import (
    Idle,
    SendPrompt,
    TurnDone,
    TurnFailedFx,
    Working,
    next_queued,
    on_command,
    on_fork_closed,
    on_fork_open,
    on_message,
)


def res(is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s1",
    )


def sysmsg(subtype: str, **data: object) -> SystemMessage:
    return SystemMessage(subtype=subtype, data={"type": "system", "subtype": subtype, **data})


def test_idle_command_starts_turn() -> None:
    state, fx = on_command(Idle(queue=()), MessageCmd("c1", "go"), turn_id="t1")
    assert state == Working(turn_id="t1", pending=frozenset(), forks=0, queue=())
    assert fx == (SendPrompt("t1", "go"),)


def test_command_while_working_is_queued() -> None:
    w = Working("t1", frozenset(), 0, ())
    state, fx = on_command(w, MessageCmd("c2", "next"), turn_id="t2")
    assert state == Working("t1", frozenset(), 0, (MessageCmd("c2", "next"),))
    assert fx == ()


def test_result_with_background_task_does_not_end_turn() -> None:
    s: Idle | Working = Working("t1", frozenset(), 0, ())
    s, _ = on_message(s, sysmsg("task_started", task_id="a", is_backgrounded=True))
    s, fx = on_message(s, res())
    assert isinstance(s, Working)
    assert fx == ()
    s, _ = on_message(s, sysmsg("task_notification", task_id="a"))
    s, fx = on_message(s, res())
    assert s == Idle(queue=())
    assert fx == (TurnDone("t1"),)


def test_background_tasks_changed_replaces_pending() -> None:
    s, _ = on_message(
        Working("t1", frozenset({"a"}), 0, ()),
        sysmsg("background_tasks_changed", tasks=[{"task_id": "b"}]),
    )
    assert isinstance(s, Working)
    assert s.pending == frozenset({"b"})


def test_error_result_fails_turn() -> None:
    s, fx = on_message(Working("t1", frozenset(), 0, ()), res(is_error=True))
    assert s == Idle(queue=())
    assert fx == (TurnFailedFx("t1", "result_error"),)


def test_result_while_idle_is_ignored() -> None:
    assert on_message(Idle(queue=()), res()) == (Idle(queue=()), ())


def test_queued_command_starts_after_done() -> None:
    s, fx = on_message(Working("t1", frozenset(), 0, (MessageCmd("c2", "next"),)), res())
    assert s == Idle(queue=(MessageCmd("c2", "next"),))
    assert fx == (TurnDone("t1"),)
    s2, fx2 = next_queued(s, turn_id="t2")
    assert s2 == Working("t2", frozenset(), 0, ())
    assert fx2 == (SendPrompt("t2", "next"),)


def test_next_queued_on_empty_queue_is_noop() -> None:
    assert next_queued(Idle(queue=()), turn_id="t2") == (Idle(queue=()), ())


def test_phase() -> None:
    w = Working("t1", frozenset(), 0, ())
    assert w.phase is Phase.WORKING
    assert on_fork_open(w).phase is Phase.AWAITING_ANSWER
    assert on_fork_closed(on_fork_open(w)).phase is Phase.WORKING
    assert Idle(queue=()).phase is Phase.IDLE


def test_negative_forks_rejected() -> None:
    with pytest.raises(ValueError, match="forks"):
        Working("t1", frozenset(), -1, ())
