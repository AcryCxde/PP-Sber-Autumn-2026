import pytest
from claude_agent_sdk import ResultMessage, SystemMessage

from ctrunner.liveness import Phase
from ctrunner.protocol import MessageCmd
from ctrunner.turn import (
    Commit,
    Committing,
    Halted,
    Idle,
    SendPrompt,
    TurnDone,
    TurnFailedFx,
    TurnState,
    Working,
    next_queued,
    on_command,
    on_commit_failed,
    on_committed,
    on_fork_closed,
    on_fork_open,
    on_message,
)

CMD = MessageCmd("c1", "go")


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
    assert state == Working(turn_id="t1", cmd=CMD, pending=frozenset(), forks=0, queue=())
    assert fx == (SendPrompt("t1", "go"),)


def test_command_while_working_is_queued() -> None:
    w = Working("t1", CMD, frozenset(), 0, ())
    state, fx = on_command(w, MessageCmd("c2", "next"), turn_id="t2")
    assert state == Working("t1", CMD, frozenset(), 0, (MessageCmd("c2", "next"),))
    assert fx == ()


def test_result_with_background_task_does_not_end_turn() -> None:
    s: TurnState = Working("t1", CMD, frozenset(), 0, ())
    s, _ = on_message(s, sysmsg("task_started", task_id="a", is_backgrounded=True))
    s, fx = on_message(s, res())
    assert isinstance(s, Working)
    assert fx == ()
    s, _ = on_message(s, sysmsg("task_notification", task_id="a"))
    s, fx = on_message(s, res())
    assert s == Committing("t1", CMD, ())
    assert fx == (Commit("t1"),)
    assert isinstance(s, Committing)
    assert on_committed(s) == (Idle(queue=()), (TurnDone("t1"),))


def test_background_tasks_changed_replaces_pending() -> None:
    s, _ = on_message(
        Working("t1", CMD, frozenset({"a"}), 0, ()),
        sysmsg("background_tasks_changed", tasks=[{"task_id": "b"}]),
    )
    assert isinstance(s, Working)
    assert s.pending == frozenset({"b"})


def test_error_result_fails_turn() -> None:
    s, fx = on_message(Working("t1", CMD, frozenset(), 0, ()), res(is_error=True))
    assert s == Idle(queue=())
    assert fx == (TurnFailedFx("t1", "result_error"),)


def test_result_while_idle_is_ignored() -> None:
    assert on_message(Idle(queue=()), res()) == (Idle(queue=()), ())


def test_queued_command_starts_after_commit() -> None:
    queued = MessageCmd("c2", "next")
    s, fx = on_message(Working("t1", CMD, frozenset(), 0, (queued,)), res())
    assert s == Committing("t1", CMD, (queued,))
    assert fx == (Commit("t1"),)
    assert isinstance(s, Committing)
    s, fx = on_committed(s)
    assert s == Idle(queue=(queued,))
    assert fx == (TurnDone("t1"),)
    assert isinstance(s, Idle)
    s2, fx2 = next_queued(s, turn_id="t2")
    assert s2 == Working("t2", queued, frozenset(), 0, ())
    assert fx2 == (SendPrompt("t2", "next"),)


def test_next_queued_on_empty_queue_is_noop() -> None:
    assert next_queued(Idle(queue=()), turn_id="t2") == (Idle(queue=()), ())


def test_phase() -> None:
    w = Working("t1", CMD, frozenset(), 0, ())
    assert w.phase is Phase.WORKING
    assert on_fork_open(w).phase is Phase.AWAITING_ANSWER
    assert on_fork_closed(on_fork_open(w)).phase is Phase.WORKING
    assert Idle(queue=()).phase is Phase.IDLE


def test_negative_forks_rejected() -> None:
    with pytest.raises(ValueError, match="forks"):
        Working("t1", CMD, frozenset(), -1, ())


def test_commit_failure_fails_turn_without_completion() -> None:
    s = Committing("t1", CMD, (MessageCmd("c2", "next"),))
    assert on_commit_failed(s, "disk_full") == (
        Idle(queue=(MessageCmd("c2", "next"),)),
        (TurnFailedFx("t1", "disk_full"),),
    )


def test_command_during_commit_is_queued() -> None:
    s, fx = on_command(Committing("t1", CMD, ()), MessageCmd("c2", "next"), turn_id="t2")
    assert s == Committing("t1", CMD, (MessageCmd("c2", "next"),))
    assert fx == ()


def test_late_message_during_commit_is_ignored() -> None:
    s = Committing("t1", CMD, ())
    assert on_message(s, res()) == (s, ())


def test_halted_waits_for_human_then_runs_oldest_queued_first() -> None:
    older = MessageCmd("c0", "old")
    halted = Halted(queue=(older,))
    assert on_message(halted, res()) == (halted, ())
    s, fx = on_command(halted, MessageCmd("c2", "new"), turn_id="t9")
    assert s == Working("t9", older, frozenset(), 0, (MessageCmd("c2", "new"),))
    assert fx == (SendPrompt("t9", "old"),)


def test_phases() -> None:
    assert Committing("t1", CMD, ()).phase is Phase.WORKING
    assert Halted(queue=()).phase is Phase.FAILED
