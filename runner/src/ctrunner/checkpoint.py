"""Подтверждённая работа: git commit в конце хода с trailer `Turn-Id`.

По trailer у `HEAD` восстановление отличает «commit успел» от «ход прерван», не угадывая.
Агент работает под тем же пользователем и может подделать trailer текущего хода; вред
ограничен его же ходом (он будет считан завершённым), чужие тома и состояние ему недоступны.
"""

import errno
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, final

GIT_TIMEOUT_S: Final = 600.0  # git add большого проекта; одновременно срок commit
TRAILER_KEY: Final = "Turn-Id"
DETAIL_LIMIT: Final = 500


def run_git(project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 — аргументы фиксированы, без shell
        ["git", "-C", str(project), *args],  # noqa: S607 — git из PATH образа
        check=check,
        capture_output=True,
        timeout=GIT_TIMEOUT_S,
    )


class CheckpointFailure(StrEnum):
    DISK_FULL = "disk_full"
    FAILED = "checkpoint_failed"


class CheckpointError(Exception):
    def __init__(self, failure: CheckpointFailure, detail: str) -> None:
        super().__init__(f"{failure.value}: {detail}")
        self.failure = failure
        self.detail = detail


@final
@dataclass(frozen=True, slots=True)
class Head:
    sha: str
    turn_id: str | None


def commit_turn(project: Path, turn_id: str) -> str:
    """Фиксирует дерево. Хуки отключены: агент не должен блокировать checkpoint."""
    try:
        run_git(project, "add", "-A")
        run_git(
            project,
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            f"checkpoint {turn_id}",
            "-m",
            f"{TRAILER_KEY}: {turn_id}",
        )
        return read_head(project).sha
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace")[:DETAIL_LIMIT]
        raise CheckpointError(_classify(detail), detail) from error
    except subprocess.TimeoutExpired as error:
        raise CheckpointError(CheckpointFailure.FAILED, "git timeout") from error
    except OSError as error:
        failure = (
            CheckpointFailure.DISK_FULL if error.errno == errno.ENOSPC else CheckpointFailure.FAILED
        )
        raise CheckpointError(failure, error.strerror or type(error).__name__) from error


def _classify(stderr: str) -> CheckpointFailure:
    if "No space left on device" in stderr:
        return CheckpointFailure.DISK_FULL
    return CheckpointFailure.FAILED


def read_head(project: Path) -> Head:
    sha = run_git(project, "rev-parse", "HEAD").stdout.decode().strip()
    trailers = run_git(
        project, "log", "-1", f"--format=%(trailers:key={TRAILER_KEY},valueonly)"
    ).stdout.decode()
    return Head(sha, trailers.strip() or None)


def clear_stale_lock(project: Path) -> bool:
    """Только на старте контейнера: git-процессов ещё нет, значит lock остался от убитого commit."""
    lock = project / ".git" / "index.lock"
    try:
        lock.unlink()
    except FileNotFoundError:
        return False
    return True
