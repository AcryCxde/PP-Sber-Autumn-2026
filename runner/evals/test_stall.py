from collections.abc import Callable

import pytest

from evals.conftest import Project, sessions


@pytest.mark.e2e
def test_stall(start_project: Callable[..., Project]) -> None:
    project = start_project("--stall-after", "30")
    project.wait("healthy", lambda: project.docker_health() == "healthy", 90)
    sessions("send", project.id, "Выполни в Bash: sleep 200 (timeout 300000), потом ответь ок")
    project.wait("stalled", lambda: project.health()["health"] == "stalled", 120)
    assert project.health()["phase"] == "working"
    # HEALTHCHECK: interval 15 с × retries 2 — docker ps показывает зависание без гейтвея.
    project.wait("unhealthy", lambda: project.docker_health() == "unhealthy", 60)
