"""Планировщик восстановления: что делать с ходом, застигнутым рестартом. Без ввода-вывода."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, assert_never, final

from ctrunner.checkpoint import Head
from ctrunner.protocol import MessageCmd
from ctrunner.state import PersistedState

MAX_AUTORESUME: Final = 3


class ResumeMode(StrEnum):
    RESUME = "resume"  # SDK-сессия продолжается по session_id
    FRESH = "fresh"  # транскрипта нет: новая сессия, задача повторяется


@final
@dataclass(frozen=True, slots=True)
class Clean:
    """Прерванного хода нет."""


@final
@dataclass(frozen=True, slots=True)
class Finalize:
    """Commit хода успел, не успели события и state: ход завершён, повторять нельзя."""

    turn_id: str
    sha: str


@final
@dataclass(frozen=True, slots=True)
class Continue:
    turn_id: str
    cmd: MessageCmd
    attempt: int
    mode: ResumeMode


@final
@dataclass(frozen=True, slots=True)
class GiveUp:
    """Лимит автопродолжений исчерпан: ход провален, дальше решает человек."""

    turn_id: str


type Recovery = Clean | Finalize | Continue | GiveUp

CLEAN: Final = Clean()


def plan(state: PersistedState, head: Head, *, resumable: bool) -> Recovery:
    turn = state.open_turn
    if turn is None:
        return CLEAN
    if head.turn_id == turn.turn_id:
        return Finalize(turn.turn_id, head.sha)
    if state.autoresume_count >= MAX_AUTORESUME:
        return GiveUp(turn.turn_id)
    mode = ResumeMode.RESUME if resumable else ResumeMode.FRESH
    return Continue(turn.turn_id, turn.cmd, state.autoresume_count + 1, mode)


def continuation_prompt(step: Continue) -> str:
    """Задача повторяется целиком в обоих режимах: модель могла не успеть её увидеть."""
    match step.mode:
        case ResumeMode.RESUME:
            preface = "Работа над задачей прервана перезапуском контейнера."
        case ResumeMode.FRESH:
            preface = (
                "Работа над задачей прервана перезапуском контейнера, "
                "контекст прежней сессии потерян."
            )
        case _:
            assert_never(step.mode)
    return (
        f"{preface} Сначала проверь `git status` и `git diff`: часть работы уже сделана, "
        f"не повторяй её.\n\nЗадача:\n{step.cmd.text}"
    )
