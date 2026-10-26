from collections.abc import Callable

import pytest

from evals.conftest import Project, docker, sessions

PROMPT = (
    "Создай файл a.txt с текстом one. Затем выполни `sleep 90`. "
    "Затем создай файл b.txt с текстом two."
)


@pytest.mark.e2e
def test_restart_mid_turn_resumes(start_project: Callable[..., Project]) -> None:
    project = start_project()
    project.wait("healthy", lambda: project.docker_health() == "healthy", 90)
    before = project.head()
    sessions("send", project.id, PROMPT)
    project.wait_event("sdk", 120, count=3)  # ход идёт
    docker("kill", project.container)
    # Ручной kill помечает контейнер остановленным человеком: политика on-failure его не поднимет.
    docker("start", project.container)
    project.wait_event("turn_resumed", 180)
    project.wait_event("turn_completed", 300)
    events = project.events()
    kinds = [e["kind"] for e in events]
    interrupted, resumed, completed = (
        kinds.index(k) for k in ("turn_interrupted", "turn_resumed", "turn_completed")
    )
    assert interrupted < resumed < completed
    checkpoint = next(e for e in events if e["kind"] == "checkpoint")
    assert isinstance(checkpoint["payload"], dict)
    assert checkpoint["payload"]["sha"] == project.head() != before
    assert docker("exec", project.container, "cat", "/workspace/project/b.txt").strip() == "two"
