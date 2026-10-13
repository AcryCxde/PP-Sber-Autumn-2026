"""Правило живости: фаза хода и время последнего прогресса → состояние здоровья."""

from enum import StrEnum
from typing import assert_never


class Phase(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    AWAITING_ANSWER = "awaiting_answer"
    FAILED = "failed"


class Health(StrEnum):
    OK = "ok"
    STALLED = "stalled"
    CRASHED = "crashed"


def assess(phase: Phase, *, last_progress_at: float, now: float, stall_after_s: float) -> Health:
    """Время монотонное и передаётся снаружи, чтобы правило оставалось чистым."""
    match phase:
        case Phase.WORKING:
            return Health.STALLED if now - last_progress_at > stall_after_s else Health.OK
        case Phase.IDLE | Phase.AWAITING_ANSWER:
            return Health.OK
        case Phase.FAILED:
            return Health.CRASHED
        case _:
            assert_never(phase)
