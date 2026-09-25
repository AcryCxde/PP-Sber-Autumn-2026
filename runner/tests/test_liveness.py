import pytest

from ctrunner.liveness import Health, Phase, assess

T = 720.0


@pytest.mark.parametrize(
    ("phase", "idle_for", "expected"),
    [
        (Phase.WORKING, 0.0, Health.OK),
        (Phase.WORKING, T, Health.OK),  # граница: ровно T — ещё ок
        (Phase.WORKING, T + 0.1, Health.STALLED),
        (Phase.IDLE, 10 * T, Health.OK),  # простой без хода — не зависание
        (Phase.AWAITING_ANSWER, 10 * T, Health.OK),  # человек думает — не зависание
        (Phase.FAILED, 0.0, Health.CRASHED),
    ],
)
def test_assess(phase: Phase, idle_for: float, expected: Health) -> None:
    health = assess(phase, last_progress_at=1000.0, now=1000.0 + idle_for, stall_after_s=T)
    assert health is expected
