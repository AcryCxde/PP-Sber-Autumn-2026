"""Подтверждённая работа: git commit в конце хода с trailer `Turn-Id`.

По trailer у `HEAD` восстановление отличает «commit успел» от «ход прерван», не угадывая.
Агент работает под тем же пользователем и может подделать trailer текущего хода; вред
ограничен его же ходом (он будет считан завершённым), чужие тома и состояние ему недоступны.
"""

import errno
import hashlib
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Final, final

from ctrunner.protocol import JsonValue

GIT_TIMEOUT_S: Final = 600.0  # git add большого проекта; одновременно срок commit
TRAILER_KEY: Final = "Turn-Id"
DETAIL_LIMIT: Final = 500
MAX_FACT_FILES: Final = 2000  # сверх лимита факты не пишутся: событие артефакта всё равно уйдёт
READ_CHUNK: Final = 1 << 20


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


@final
@dataclass(frozen=True, slots=True)
class FileFact:
    """Размер и sha256 содержимого файла в коммите."""

    size_bytes: int
    sha256: str


def checkpoint_payload(sha: str, facts: Mapping[str, FileFact]) -> dict[str, JsonValue]:
    """Запись `checkpoint` журнала; `files` только если факты есть (старые записи не меняются)."""
    payload: dict[str, JsonValue] = {"sha": sha}
    if facts:
        payload["files"] = {
            path: {"size_bytes": f.size_bytes, "sha256": f.sha256} for path, f in facts.items()
        }
    return payload


def read_file_facts(project: Path, sha: str) -> dict[str, FileFact]:
    """Факты о файлах, добавленных или изменённых коммитом `sha`, по содержимому коммита.

    Читается объект git, а не рабочее дерево: оно после commit могло измениться. Путь, который
    нельзя безопасно передать git (перевод строки, не UTF-8), и не-файл (submodule) пропускаются.
    Сбой git или чтения — исключение: вызывающий решает, терять ли факты (событие они не отменяют).
    """
    raw = run_git(
        project, "diff-tree", "--root", "-r", "-z", "--no-renames", "--diff-filter=AM",
        "--name-only", "--no-commit-id", sha,
    ).stdout  # fmt: skip
    paths = [p for p in (_decode_path(x) for x in raw.split(b"\0")) if p is not None]
    return _blob_facts(project, sha, paths[:MAX_FACT_FILES])


def _decode_path(raw: bytes) -> str | None:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return path if path and "\n" not in path else None


def _blob_facts(project: Path, sha: str, paths: list[str]) -> dict[str, FileFact]:
    if not paths:
        return {}
    with subprocess.Popen(  # noqa: S603 — аргументы фиксированы, без shell
        ["git", "-C", str(project), "cat-file", "--batch"],  # noqa: S607 — git из PATH образа
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as proc:
        timer = threading.Timer(GIT_TIMEOUT_S, proc.kill)  # зависший git не держит commit-поток
        timer.start()
        try:
            if proc.stdin is None or proc.stdout is None:
                raise OSError("git cat-file: pipes are not available")
            facts: dict[str, FileFact] = {}
            for path in paths:
                fact = _one_blob(proc.stdin, proc.stdout, f"{sha}:{path}")
                if fact is not None:
                    facts[path] = fact
            return facts
        finally:
            timer.cancel()
            proc.kill()  # после чтения всех ответов процессу нечего делать; pipes закроет `with`


def _one_blob(stdin: IO[bytes], stdout: IO[bytes], name: str) -> FileFact | None:
    stdin.write(f"{name}\n".encode())
    stdin.flush()
    header = stdout.readline().split()
    if len(header) == 2 and header[1] == b"missing":
        return None
    if len(header) != 3 or not header[2].isdigit():
        raise OSError("git cat-file: unexpected output")
    is_blob, size = header[1] == b"blob", int(header[2])
    digest, left = hashlib.sha256(), size
    while left:
        chunk = stdout.read(min(left, READ_CHUNK))
        if not chunk:
            raise OSError("git cat-file: truncated output")
        digest.update(chunk)
        left -= len(chunk)
    if stdout.read(1) != b"\n":
        raise OSError("git cat-file: malformed object frame")
    return FileFact(size, digest.hexdigest()) if is_blob else None
