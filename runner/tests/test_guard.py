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
        ("Glob", {"pattern": "*.py", "path": "src"}),
        ("Glob", {"pattern": "src/*/../lib/*.py"}),
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
        ("Glob", {"pattern": "../.runner/*"}, "/workspace/.runner"),
        ("Glob", {"pattern": "../claude/**", "path": "."}, "/workspace/claude"),
        ("Glob", {"pattern": "src/**/../../../.runner/*"}, "/"),
        ("Glob", {"pattern": "{../../.runner,src}/*"}, "/"),
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


@pytest.mark.parametrize(
    ("command", "path"),
    [
        ("echo x>/etc/foo", "/etc/foo"),
        ("cat</etc/passwd", "/etc/passwd"),
        ("echo x>>/etc/foo", "/etc/foo"),
        ("echo x 2>/run/secrets/error", "/run/secrets/error"),
        ("ls&&cat</etc/passwd", "/etc/passwd"),
        ("false||cat</etc/passwd", "/etc/passwd"),
        ("true;cat</run/secrets/t", "/run/secrets/t"),
        ("cat</etc/passwd|wc -c", "/etc/passwd"),
        ("(cat</etc/passwd)", "/etc/passwd"),
        ("cat<../proj-other/a", "/workspace/proj-other/a"),
        ('cat<"/workspace/proj-other/secret file.md"', "/workspace/proj-other/secret file.md"),
    ],
)
def test_bash_denies_paths_glued_to_shell_operators(command: str, path: str) -> None:
    # Проверяется только решение guard: shell-команды не исполняются.
    verdict = check("Bash", {"command": command}, PROJECT, POLICY, ident)
    assert isinstance(verdict, Deny)
    assert verdict.path == path


@pytest.mark.parametrize(
    "command",
    [
        "echo x>/workspace/project/out.txt",
        "cat</workspace/project/in.txt|wc -c",
        "echo x>>/tmp/out.txt",
        "echo x 2>/dev/null",
        "true&&cat</workspace/project/in.txt",
        "false||cat</workspace/project/in.txt",
        "true;cat</workspace/project/in.txt",
        "(cat</workspace/project/in.txt)",
        'cat<"/workspace/project/input file.txt"',
        "cat<notes/in.txt",
        "echo x>/workspace/project/out.txt 2>&1",
    ],
)
def test_bash_allows_permitted_paths_glued_to_shell_operators(command: str) -> None:
    assert check("Bash", {"command": command}, PROJECT, POLICY, ident) == Allow()


def test_bash_unbalanced_quotes_falls_back() -> None:
    assert isinstance(check("Bash", {"command": "cat '/etc/passwd"}, PROJECT, POLICY, ident), Deny)


def test_bash_unknown_home_is_literal_relative_path() -> None:
    # shell оставляет ~несуществующий буквальным, значит это путь внутри cwd
    verdict = check("Bash", {"command": "cat ~no-such-user-x/a"}, PROJECT, POLICY, ident)
    assert verdict == Allow()
