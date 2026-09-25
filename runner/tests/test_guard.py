from pathlib import Path

import pytest

from ctrunner.guard import Allow, Deny, Policy, check

PROJECT = Path("/workspace/project")
OTHER = "/workspace/proj-other/secret.md"
EVENTS = "/workspace/.runner/events.jsonl"
POLICY = Policy(
    roots=(PROJECT, Path("/tmp")),
    bash_system=(Path("/usr"), Path("/bin"), Path("/lib"), Path("/dev/null")),
)


def ident(p: Path) -> Path:
    return p


def symlink_escape(p: Path) -> Path:
    # имитация realpath: project/link → /workspace/other, остальные пути без изменений
    link = PROJECT / "link"
    return Path("/workspace/other") / p.relative_to(link) if p.is_relative_to(link) else p


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "/workspace/project/a.md"}),
        ("Read", {"file_path": "notes/a.md"}),
        ("Write", {"file_path": "/tmp/x"}),
        ("Glob", {"pattern": "**/*.py"}),
        ("Grep", {"pattern": "x", "path": "src"}),
        ("Bash", {"command": "ls -la && git status"}),
        ("Bash", {"command": "/usr/bin/env python -V > /dev/null"}),
        ("Agent", {"prompt": "read /etc/passwd"}),  # не файловый инструмент
    ],
)
def test_allows(tool: str, tool_input: dict[str, object]) -> None:
    assert check(tool, tool_input, PROJECT, POLICY, ident) == Allow()


@pytest.mark.parametrize(
    ("tool", "tool_input", "path"),
    [
        ("Read", {"file_path": OTHER}, OTHER),
        ("Read", {"file_path": "../proj-other/a"}, "/workspace/proj-other/a"),
        ("Read", {"file_path": EVENTS}, EVENTS),
        ("Edit", {"file_path": "/etc/passwd"}, "/etc/passwd"),
        ("Glob", {"pattern": "/workspace/claude/**"}, "/workspace/claude"),
        ("Grep", {"pattern": "k", "path": "/run/secrets"}, "/run/secrets"),
        ("Read", {"file_path": "link/x"}, "/workspace/other/x"),
        ("Bash", {"command": "cat ../proj-other/a"}, "/workspace/proj-other/a"),
        ("Bash", {"command": "cat /etc/passwd"}, "/etc/passwd"),
        ("Bash", {"command": "cp x --target=/workspace/claude"}, "/workspace/claude"),
    ],
)
def test_denies(tool: str, tool_input: dict[str, object], path: str) -> None:
    verdict = check(tool, tool_input, PROJECT, POLICY, symlink_escape)
    assert isinstance(verdict, Deny)
    assert verdict.path == path


def test_bash_unbalanced_quotes_falls_back() -> None:
    assert isinstance(check("Bash", {"command": "cat '/etc/passwd"}, PROJECT, POLICY, ident), Deny)


def test_bash_unknown_home_is_literal_relative_path() -> None:
    # shell оставляет ~несуществующий буквальным, значит это путь внутри cwd
    verdict = check("Bash", {"command": "cat ~no-such-user-x/a"}, PROJECT, POLICY, ident)
    assert verdict == Allow()
