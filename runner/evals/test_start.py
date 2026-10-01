from collections.abc import Callable

import pytest

from evals.conftest import Project, sessions


@pytest.mark.e2e
def test_start(start_project: Callable[..., Project]) -> None:
    project = start_project()
    project.wait("healthy", lambda: project.docker_health() == "healthy", 90)
    sessions("send", project.id, "Ответь одним словом: ок")
    project.wait_event("turn_completed", 120)
    kinds = [e["kind"] for e in project.events()]
    assert kinds[0] == "session_started"
    assert kinds.index("turn_started") < kinds.index("turn_completed")
    assert "sdk" in kinds[kinds.index("turn_started") :]
