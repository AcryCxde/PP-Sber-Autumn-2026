import pytest

from ctrunner.checkpoint import Head
from ctrunner.protocol import MessageCmd
from ctrunner.recovery import (
    MAX_AUTORESUME,
    Clean,
    Continue,
    Finalize,
    GiveUp,
    ResumeMode,
    continuation_prompt,
    plan,
)
from ctrunner.state import EMPTY, OpenTurn, PersistedState

CMD = MessageCmd("c1", "собери отчёт")
SHA = "a" * 40


def interrupted(count: int = 0) -> PersistedState:
    return PersistedState("sess-1", OpenTurn("t1", CMD), None, count, ())


def test_no_open_turn_is_clean() -> None:
    assert plan(EMPTY, Head(SHA, None), resumable=True) == Clean()


def test_commit_of_open_turn_means_finalize_even_at_limit() -> None:
    state = interrupted(MAX_AUTORESUME)
    assert plan(state, Head(SHA, "t1"), resumable=True) == Finalize("t1", SHA)


def test_other_turn_trailer_does_not_finalize() -> None:
    assert isinstance(plan(interrupted(), Head(SHA, "t0"), resumable=True), Continue)


@pytest.mark.parametrize(
    ("resumable", "mode"), [(True, ResumeMode.RESUME), (False, ResumeMode.FRESH)]
)
def test_continue_mode_follows_transcript(resumable: bool, mode: ResumeMode) -> None:
    result = plan(interrupted(), Head(SHA, None), resumable=resumable)
    assert result == Continue("t1", CMD, 1, mode)


def test_attempt_counts_up_to_limit_then_gives_up() -> None:
    last = plan(interrupted(MAX_AUTORESUME - 1), Head(SHA, None), resumable=True)
    assert isinstance(last, Continue)
    assert last.attempt == MAX_AUTORESUME
    assert plan(interrupted(MAX_AUTORESUME), Head(SHA, None), resumable=True) == GiveUp("t1")


@pytest.mark.parametrize("mode", list(ResumeMode))
def test_prompt_keeps_task_and_asks_to_check_tree(mode: ResumeMode) -> None:
    text = continuation_prompt(Continue("t1", CMD, 1, mode))
    assert CMD.text in text
    assert "git status" in text
    assert "git diff" in text
