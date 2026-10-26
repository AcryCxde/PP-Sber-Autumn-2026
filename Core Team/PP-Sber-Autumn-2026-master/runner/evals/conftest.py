"""E2E: нужны Docker, образ coreteams-runner:dev и окружение шлюза (source env.zsh)."""

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "demo"
SECRETS_ROOT = Path.home() / ".config" / "coreteams" / "secrets"
POLL_S = 2.0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    missing = [
        why
        for why, ok in [
            ("docker not found", shutil.which("docker") is not None),
            ("gateway env not sourced", bool(os.environ.get("ANTHROPIC_AUTH_TOKEN"))),
        ]
        if not ok
    ]
    if missing:
        for item in items:
            item.add_marker(pytest.mark.skip(reason=", ".join(missing)))


def sessions(*args: str) -> str:
    return subprocess.run(["sessions", *args], check=True, capture_output=True, text=True).stdout


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


@dataclass(frozen=True, slots=True)
class Project:
    id: str

    @property
    def container(self) -> str:
        return f"ct-{self.id}"

    def events(self) -> list[dict[str, object]]:
        raw = docker("exec", self.container, "cat", "/workspace/.runner/events.jsonl")
        return [json.loads(line) for line in raw.splitlines()]

    def health(self) -> dict[str, object]:
        doc: dict[str, object] = json.loads(
            docker("exec", self.container, "cat", "/run/ctrunner/health.json")
        )
        return doc

    def docker_health(self) -> str:
        fmt = "{{.State.Health.Status}}"
        return docker("inspect", "--format", fmt, self.container).strip()

    def head(self) -> str:
        return docker(
            "exec", self.container, "git", "-C", "/workspace/project", "rev-parse", "HEAD"
        ).strip()

    def wait(self, what: str, ready: Callable[[], bool], timeout_s: float) -> None:
        """Опрос внешней системы с дедлайном: другого сигнала у контейнера нет."""

        def ready_or_down() -> bool:
            # Между kill и рестартом контейнер не отвечает на `docker exec`: «ещё не готово».
            try:
                return ready()
            except subprocess.CalledProcessError:
                return False

        deadline = time.monotonic() + timeout_s
        while not ready_or_down():
            if time.monotonic() > deadline:
                pytest.fail(f"{self.id}: {what} not reached in {timeout_s:g}s")
            time.sleep(POLL_S)

    def wait_event(self, kind: str, timeout_s: float, count: int = 1) -> None:
        self.wait(kind, lambda: sum(e["kind"] == kind for e in self.events()) >= count, timeout_s)


@pytest.fixture
def start_project() -> Iterator[Callable[..., Project]]:
    started: list[str] = []

    def start(*extra: str) -> Project:
        project_id = f"ev-{uuid.uuid4().hex[:8]}"
        started.append(project_id)
        sessions("secrets", "init", project_id)
        sessions("start", project_id, "--data", str(FIXTURE), *extra)
        return Project(project_id)

    yield start
    for project_id in started:
        subprocess.run(["sessions", "rm", project_id, "--volume"], check=False, capture_output=True)
        shutil.rmtree(SECRETS_ROOT / project_id, ignore_errors=True)
