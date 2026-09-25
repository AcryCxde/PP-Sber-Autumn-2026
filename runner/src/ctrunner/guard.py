"""Сторож PreToolUse: пути инструментов не должны выходить за разрешённые корни.

Для файловых инструментов проверка точная: путь нормализуется и раскрываются symlink-и.
Для Bash это только эвристика по абсолютным путям, `~` и `..` в токенах команды — она ловит
очевидные попытки и пишет их в журнал, но **не является границей безопасности**:
переменные, подстановки и интерпретаторы её обходят. Настоящая граница — то, что
смонтировано в контейнер.
"""

import os
import shlex
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from itertools import takewhile
from pathlib import Path
from typing import Final, final


@final
@dataclass(frozen=True, slots=True)
class Allow:
    pass


@final
@dataclass(frozen=True, slots=True)
class Deny:
    path: str
    reason: str


type Verdict = Allow | Deny


@final
@dataclass(frozen=True, slots=True)
class Policy:
    roots: tuple[Path, ...]  # файловые инструменты и Bash
    bash_system: tuple[Path, ...]  # дополнительно разрешено только в Bash


PATH_FIELDS: Final[Mapping[str, str]] = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "path",
    "Grep": "path",
    "LS": "path",
}
GLOB_CHARS: Final = frozenset("*?[{")
SHELL_PUNCT: Final = "<>|&;()'\""


def check(
    tool: str,
    tool_input: Mapping[str, object],
    cwd: Path,
    policy: Policy,
    resolve: Callable[[Path], Path],
) -> Verdict:
    if tool == "Bash":
        command = tool_input.get("command")
        candidates = _bash_paths(command) if isinstance(command, str) else iter(())
        allowed = policy.roots + policy.bash_system
    elif tool in PATH_FIELDS:
        candidates = _tool_paths(tool, tool_input)
        allowed = policy.roots
    else:
        return Allow()
    for raw in candidates:
        path = resolve(_normalize(raw, cwd))
        if not any(path == root or path.is_relative_to(root) for root in allowed):
            return Deny(str(path), f"доступ за пределами проекта запрещён: {path}")
    return Allow()


def _normalize(raw: str, cwd: Path) -> Path:
    try:
        expanded = Path(raw).expanduser()
    except RuntimeError:
        # Неизвестный ~user: shell тоже оставит его буквальным относительным путём.
        expanded = Path(raw)
    return Path(os.path.normpath(cwd / expanded))


def _tool_paths(tool: str, tool_input: Mapping[str, object]) -> Iterator[str]:
    value = tool_input.get(PATH_FIELDS[tool])
    if isinstance(value, str):
        yield value
    pattern = tool_input.get("pattern")
    if tool == "Glob" and isinstance(pattern, str) and pattern.startswith("/"):
        yield _glob_base(pattern)


def _glob_base(pattern: str) -> str:
    static = takewhile(lambda part: not GLOB_CHARS.intersection(part), Path(pattern).parts)
    return str(Path(*static))


def _bash_paths(command: str) -> Iterator[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        candidate = token.lstrip(SHELL_PUNCT).rpartition("=")[2]
        if candidate.startswith(("/", "~")) or ".." in candidate:
            yield candidate
