"""Негативная матрица изоляции проектов на стороне runner (PPS-132).

Граница изоляции: том `proj-<id>` монтируется только в свой контейнер, внутри него сторож
(`guard.check`) ограничивает файловые инструменты корнем `/workspace/project`, команды
принимает `accept_command` + `Inbox`, наружу уходят только события v0 после `Redactor`.

| Группа | Вектор                                             | Слой                   | Итог     |
|--------|----------------------------------------------------|------------------------|----------|
| PATH   | `../`, абсолютный путь, `~`, `/proc/self/environ`, | guard.check            | Deny     |
|        | `/run/secrets/*`, `.runner`, `claude`, соседний    |                        |          |
|        | том, двойник корня, `/usr/../etc`, NUL-байт        |                        |          |
| PATH   | симлинк на файл/каталог, цепочка, висячий (Write), | guard.check + realpath | Deny     |
|        | симлинк из seed                                    |                        |          |
| PATH   | `..\\x`, `%2e%2e/x`, юникод-точки (буквальные)     | guard.check            | Allow внутри корня |
| PATH   | hardlink на внешний файл                           | guard.check            | XFAIL    |
| BASH   | `cd /`, `$(...)`, редирект (в т.ч. приклеенный)    | эвристика Bash         | Deny     |
| BASH   | `$HOME`, base64-подстановка, динамический путь     | эвристика Bash         | XFAIL    |
| CMD    | плохой `id`, чужой `fork_id`, чужие вопросы,       | accept_command, Inbox  | rejected, inbox пуст |
|        | ответ на закрытую развилку, подмена по тому же id  |                        |          |
| CMD    | чужой `project_id` в кадре команды                 | accept_command         | не пишется; reject XFAIL |
| ID     | `project_id` вне `[a-z0-9][a-z0-9-]{0,39}`         | RunnerConfig, sessions | отказ    |
| DOCKER | чужие тома, docker.sock, привилегии, секреты в env | sessions.run_args      | нет в аргументах |
| LEAK   | токены, `/run/secrets`, `.runner`, env, чужой id   | EventLog → Translator  | нет в draft'ах v0 |
| LEAK   | путь в тексте модели, base64/hex токена            | Translator, Redactor   | XFAIL    |

Каждый отказ проверяется вместе с отсутствием побочных эффектов: файл не создан,
inbox пуст, журнал и события без утечки. Всё, что не отклоняется, помечено
`xfail(strict=True)` и описано в `docs/2.4/isolation-matrix.md`.
"""  # noqa: E501 — таблица матрицы

import base64
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from ctrunner.config import ConfigError, RunnerConfig
from ctrunner.eventlog import Event, EventLog
from ctrunner.gateway import accept_command
from ctrunner.guard import Allow, Deny, Policy, check
from ctrunner.inbox import PROCESSED, REJECTED, Inbox, answers_for, put
from ctrunner.main import seed_project
from ctrunner.protocol import EventKind, JsonValue
from ctrunner.redact import Redactor
from ctrunner.sessions import RunSpec, container_env, run_args
from ctrunner.sessions import cli as sessions_cli
from ctrunner.v0 import Draft, Translator, read_journal

ANTHROPIC_TOKEN = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA"
GATEWAY_TOKEN = "gw-tok-BBBBBBBBBBBBBBBBBBBBBBBB"
TOKENS = (ANTHROPIC_TOKEN, GATEWAY_TOKEN)
OWN = "demo-a"
FOREIGN = "demo-b"


class Workspace:
    """Дерево тома: проект, служебный каталог, соседний проект и файл вне тома."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.project = root / "project"
        self.runner = root / ".runner"
        self.claude = root / "claude"
        self.other = root / "proj-other"
        self.outside = root / "outside"
        for d in (self.project, self.runner / "inbox", self.claude, self.other, self.outside):
            d.mkdir(parents=True)
        (self.project / "a.md").write_text("свой файл", encoding="utf-8")
        (self.runner / "events.jsonl").write_text("{}\n", encoding="utf-8")
        (self.other / "secret.md").write_text("чужой файл", encoding="utf-8")
        (self.outside / "secret.txt").write_text("вне тома", encoding="utf-8")
        self.policy = Policy(roots=(self.project,), bash_system=())

    def verdict(self, tool: str, tool_input: Mapping[str, object]) -> object:
        return check(
            tool, tool_input, self.project, self.policy, lambda p: Path(os.path.realpath(p))
        )

    def snapshot(self) -> dict[str, str]:
        return {
            str(p.relative_to(self.root)): p.read_text(encoding="utf-8") if p.is_file() else "<dir>"
            for p in sorted(self.root.rglob("*"))
        }


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path.resolve() / "workspace")


# --- PATH: отказы ---------------------------------------------------------------------------

type Build = Callable[[Workspace], tuple[str, dict[str, object]]]


def _link(ws: Workspace, name: str, target: Path) -> str:
    (ws.project / name).symlink_to(target)
    return name


def _chain(ws: Workspace) -> str:
    _link(ws, "ln-a", ws.outside / "secret.txt")
    return _link(ws, "ln-b", ws.project / "ln-a")


PATH_DENIED: dict[str, Build] = {
    "dotdot-sibling-volume": lambda ws: ("Read", {"file_path": "../proj-other/secret.md"}),
    "dotdot-deep": lambda ws: ("Read", {"file_path": "a/b/../../../outside/secret.txt"}),
    "absolute-sibling-volume": lambda ws: ("Read", {"file_path": str(ws.other / "secret.md")}),
    "absolute-etc-passwd": lambda ws: ("Read", {"file_path": "/etc/passwd"}),
    "proc-self-environ": lambda ws: ("Read", {"file_path": "/proc/self/environ"}),
    "run-secrets-token": lambda ws: ("Read", {"file_path": "/run/secrets/anthropic_token"}),
    "run-secrets-grep": lambda ws: ("Grep", {"pattern": "sk-", "path": "/run/secrets"}),
    "run-secrets-glob": lambda ws: ("Glob", {"pattern": "/run/secrets/*"}),
    "runner-events": lambda ws: ("Read", {"file_path": str(ws.runner / "events.jsonl")}),
    "runner-inbox-write": lambda ws: (
        "Write",
        {"file_path": str(ws.runner / "inbox" / "evil.json"), "content": "{}"},
    ),
    "claude-dir-edit": lambda ws: ("Edit", {"file_path": str(ws.claude / "settings.json")}),
    "root-prefix-twin": lambda ws: ("Read", {"file_path": f"{ws.project}-evil/x"}),
    "home-tilde": lambda ws: ("Read", {"file_path": "~/.ssh/id_rsa"}),
    "glob-escape": lambda ws: ("Glob", {"pattern": "../proj-other/*"}),
    "glob-escape-recursive": lambda ws: ("Glob", {"pattern": "**/../../outside/*"}),
    "symlink-file": lambda ws: (
        "Read",
        {"file_path": _link(ws, "ln-file", ws.outside / "secret.txt")},
    ),
    "symlink-dir": lambda ws: ("Read", {"file_path": _link(ws, "ln-dir", ws.other) + "/secret.md"}),
    "symlink-dangling-write": lambda ws: (
        "Write",
        {"file_path": _link(ws, "ln-new", ws.outside / "created.txt"), "content": "x"},
    ),
    "symlink-chain": lambda ws: ("Read", {"file_path": _chain(ws)}),
    "bash-dotdot": lambda ws: ("Bash", {"command": "cat ../proj-other/secret.md"}),
    "bash-absolute": lambda ws: ("Bash", {"command": "cat /run/secrets/anthropic_token"}),
    "bash-proc": lambda ws: ("Bash", {"command": "cat /proc/self/environ | tr '\\0' '\\n'"}),
    "bash-inbox-redirect": lambda ws: (
        "Bash",
        {"command": f"echo '{{}}' > {ws.runner}/inbox/evil.json"},
    ),
    "bash-usr-dotdot": lambda ws: ("Bash", {"command": "cat /usr/../etc/passwd"}),
    "bash-dev-null-dotdot": lambda ws: ("Bash", {"command": "cat /dev/null/../../etc/passwd"}),
    "bash-cd-root": lambda ws: ("Bash", {"command": "cd / && cat etc/passwd"}),
    "bash-substitution": lambda ws: ("Bash", {"command": "cat $(echo /etc/passwd)"}),
    "bash-interpreter-literal": lambda ws: (
        "Bash",
        {"command": "python3 -c \"print(open('/etc/passwd').read())\""},
    ),
    "bash-tilde": lambda ws: ("Bash", {"command": "cat ~/.config/coreteams/secrets/x"}),
    "bash-option-value": lambda ws: ("Bash", {"command": "cp a --target-directory=/run/secrets"}),
}


@pytest.mark.parametrize("name", PATH_DENIED)
def test_path_vector_denied_without_side_effects(ws: Workspace, name: str) -> None:
    tool, tool_input = PATH_DENIED[name](ws)
    before = ws.snapshot()
    assert isinstance(ws.verdict(tool, tool_input), Deny)
    assert ws.snapshot() == before  # сторож ничего не создаёт и не меняет
    assert not (ws.outside / "created.txt").exists()
    assert list((ws.runner / "inbox").iterdir()) == []


def test_path_nul_byte_fails_closed(ws: Workspace) -> None:
    # Исключение сторожа Session превращает в Deny (`Session._verdict`): вызов не проходит.
    with pytest.raises(ValueError, match="null"):
        ws.verdict("Read", {"file_path": "a\0b"})


@pytest.mark.parametrize(
    "name",
    ["..\\outside\\secret.txt", "%2e%2e/outside/secret.txt", "．．/outside", "‥/outside", "..%2f"],
)
def test_literal_names_stay_inside_root(ws: Workspace, name: str) -> None:
    # Read не декодирует `%2e`, не нормализует юникод и не считает `\` разделителем:
    # это обычное имя внутри проекта, а не выход из него.
    assert ws.verdict("Read", {"file_path": name}) == Allow()
    assert Path(os.path.realpath(ws.project / name)).is_relative_to(ws.project)


def test_seed_symlink_cannot_reach_outside(ws: Workspace, tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "leak").symlink_to(ws.outside / "secret.txt")
    (seed / "ok.md").write_text("материал", encoding="utf-8")
    project = ws.root / "seeded"
    seed_project(project, seed=seed, ctf=tmp_path / "no-ctf")
    policy = Policy(roots=(project,), bash_system=())
    resolve = lambda p: Path(os.path.realpath(p))  # noqa: E731
    assert isinstance(check("Read", {"file_path": "leak"}, project, policy, resolve), Deny)
    assert check("Read", {"file_path": "ok.md"}, project, policy, resolve) == Allow()


# --- PATH / BASH: найденные дыры (xfail strict) ----------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="hardlink неотличим от обычного файла: realpath не раскрывает его, "
    "граница — раздельные тома (EXDEV) и fs.protected_hardlinks, а не сторож",
)
def test_hardlink_to_outside_file_denied(ws: Workspace) -> None:
    os.link(ws.outside / "secret.txt", ws.project / "hard")
    assert isinstance(ws.verdict("Read", {"file_path": "hard"}), Deny)


BASH_HEURISTIC_BYPASS = {
    "env-var": "cat $HOME/.config/coreteams/secrets/x",
    "encoded-path": "cat $(echo L2V0Yy9wYXNzd2Q= | base64 -d)",
    "interpreter-built-path": "python3 -c \"import os; open(os.sep + 'etc' + os.sep + 'passwd')\"",
}


@pytest.mark.xfail(
    strict=True,
    reason="Bash-сторож — эвристика по токенам (guard.py, docstring): переменные, cd, "
    "подстановки и интерпретаторы её обходят; настоящая граница — состав монтирования",
)
@pytest.mark.parametrize("command", BASH_HEURISTIC_BYPASS.values(), ids=BASH_HEURISTIC_BYPASS)
def test_bash_heuristic_bypass_denied(ws: Workspace, command: str) -> None:
    assert isinstance(ws.verdict("Bash", {"command": command}), Deny)


@pytest.mark.parametrize(
    "command",
    ["echo x>/etc/foo", "cat<</etc/passwd", "ls&&cat</etc/passwd", "true;cat</run/secrets/t"],
)
def test_bash_glued_redirect_denied(ws: Workspace, command: str) -> None:
    assert isinstance(ws.verdict("Bash", {"command": command}), Deny)


# --- CMD: команды gateway и inbox ------------------------------------------------------------

INBOX_JSON = "[!.]*.json"


def inbox_files(directory: Path) -> list[Path]:
    return [*directory.rglob("*.json")]


def forks(known: Mapping[str, tuple[str, ...]]) -> Callable[[str], tuple[str, ...] | None]:
    return known.get


def command(**fields: object) -> dict[str, object]:
    return {"v": 1, "type": "command", **fields}


BAD_IDS = {
    "empty": "",
    "space": "a b",
    "newline": "a\nb",
    "header-injection": "a\r\nTurn-Id: evil",
    "nul": "a\0b",
    "traversal": "../../x",
    "slash": "a/b",
    "too-long": "x" * 65,
    "cyrillic": "идентификатор",
    "fullwidth-digits": "１２３",
}


@pytest.mark.parametrize("raw_id", BAD_IDS.values(), ids=BAD_IDS)
@pytest.mark.parametrize("kind", ["message", "stop", "fork_answer"])
def test_command_with_bad_id_rejected(tmp_path: Path, raw_id: str, kind: str) -> None:
    frame = command(id=raw_id, kind=kind, text="x", fork_id="f1", answers={"q": "a"})
    ack = accept_command(frame, inbox_dir=tmp_path, fork_questions=forks({"f1": ("q",)}))
    assert ack["status"] == "rejected"
    assert inbox_files(tmp_path) == []


@pytest.mark.parametrize(
    "fork_id", ["foreign-fork", "../f1", "f1\n", "F1", " f1", "f1\0", "f1/../f1"]
)
def test_fork_answer_to_foreign_fork_rejected(tmp_path: Path, fork_id: str) -> None:
    frame = command(id="c1", kind="fork_answer", fork_id=fork_id, answers={"q": "a"})
    ack = accept_command(frame, inbox_dir=tmp_path, fork_questions=forks({"f1": ("q",)}))
    assert ack == {"v": 1, "type": "command_ack", "id": "c1", "status": "rejected",
                   "reason": "unknown_fork"}  # fmt: skip
    assert inbox_files(tmp_path) == []


@pytest.mark.parametrize(
    "answers",
    [{}, {"other": "a"}, {"q": "a", "extra": "b"}, {"Q": "a"}],
    ids=["empty", "foreign-question", "extra-question", "case"],
)
def test_fork_answer_with_foreign_questions_rejected(
    tmp_path: Path, answers: dict[str, str]
) -> None:
    frame = command(id="c1", kind="fork_answer", fork_id="f1", answers=answers)
    ack = accept_command(frame, inbox_dir=tmp_path, fork_questions=forks({"f1": ("q",)}))
    assert ack["status"] == "rejected"
    assert ack["reason"] == "answers_mismatch"
    assert inbox_files(tmp_path) == []


def test_answer_to_closed_fork_rejected(tmp_path: Path) -> None:
    frame = command(id="c1", kind="fork_answer", fork_id="f1", answers={"q": "a"})
    ack = accept_command(frame, inbox_dir=tmp_path, fork_questions=forks({}))
    assert ack["reason"] == "unknown_fork"
    assert inbox_files(tmp_path) == []


def test_foreign_project_id_in_frame_is_not_persisted(tmp_path: Path) -> None:
    frame = command(id="c1", kind="message", text="привет", project_id=FOREIGN, run_id="r9")
    assert accept_command(frame, inbox_dir=tmp_path, fork_questions=forks({}))["status"] == (
        "accepted"
    )
    (written,) = inbox_files(tmp_path)
    assert json.loads(written.read_text(encoding="utf-8")) == {
        "id": "c1", "kind": "message", "text": "привет"
    }  # fmt: skip


@pytest.mark.xfail(
    strict=True,
    reason="accept_command не знает project_id runner'а и принимает кадр с чужим project_id "
    "(gateway.py, accept_command): изоляция держится только на маршрутизации gateway",
)
def test_frame_with_foreign_project_id_rejected(tmp_path: Path) -> None:
    frame = command(id="c1", kind="message", text="x", project_id=FOREIGN)
    ack = accept_command(frame, inbox_dir=tmp_path, fork_questions=forks({}))
    assert ack["status"] == "rejected"
    assert inbox_files(tmp_path) == []


def test_replayed_id_with_other_body_does_not_overwrite(tmp_path: Path) -> None:
    first = command(id="c1", kind="message", text="исходная")
    replay = command(id="c1", kind="message", text="подмена")
    accept_command(first, inbox_dir=tmp_path, fork_questions=forks({}))
    ack = accept_command(replay, inbox_dir=tmp_path, fork_questions=forks({}))
    assert ack["status"] == "accepted"
    (written,) = inbox_files(tmp_path)
    assert json.loads(written.read_text(encoding="utf-8"))["text"] == "исходная"


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "c1", "kind": "bogus"},
        {"id": "c1\n", "kind": "stop"},
        {"id": "c1", "kind": "fork_answer", "fork_id": 7, "answers": {}},
        {"id": "c1", "kind": "fork_answer", "fork_id": "f", "answers": {"q": 1}},
        {"id": "../c1", "kind": "message", "text": "x"},
    ],
    ids=["kind", "id-newline", "fork-int", "answer-int", "id-traversal"],
)
def test_inbox_rejects_foreign_files_without_delivery(
    tmp_path: Path, raw: dict[str, JsonValue]
) -> None:
    inbox = Inbox(tmp_path)
    put(tmp_path, raw)
    assert inbox.peek() == []
    assert list(tmp_path.glob(INBOX_JSON)) == []
    assert len(list((tmp_path / REJECTED).glob("*.json"))) == 1
    assert list((tmp_path / PROCESSED).glob("*.json")) == []


@pytest.mark.parametrize("fork_id", ["foreign", "F1", "f1 ", "../f1", "f1\n"])
def test_cli_answers_for_foreign_fork_unknown(tmp_path: Path, fork_id: str) -> None:
    events = tmp_path / "events.jsonl"
    row = {"seq": 1, "kind": EventKind.FORK_QUESTION,
           "payload": {"fork_id": "f1", "questions": [{"question": "q"}]}}  # fmt: skip
    events.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown fork"):
        answers_for(events, fork_id, ["a"])


# --- ID / DOCKER ---------------------------------------------------------------------------

BAD_PROJECT_IDS = ["", "../x", "A", "a_b", "-a", "a b", "a\n", "a/b", "x" * 41, "проект", "a;rm"]


def runner_config(project_id: str) -> RunnerConfig:
    return RunnerConfig(
        project_id=project_id,
        anthropic_base_url="https://gw.example",
        anthropic_token=ANTHROPIC_TOKEN,
        models={},
        stall_after_s=720.0,
        workspace=Path("/workspace"),
    )


@pytest.mark.parametrize("project_id", BAD_PROJECT_IDS)
def test_bad_project_id_rejected_by_config_and_sessions(
    project_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigError, match="PROJECT_ID"):
        runner_config(project_id)
    with pytest.raises(ValueError, match="project_id"):
        RunSpec(project_id, "img", Path("/s"), None, {}, 1.0)
    monkeypatch.setattr(sys, "argv", ["sessions", "stop", project_id])
    with pytest.raises(SystemExit):
        sessions_cli()


def spec(project_id: str = OWN, env: Mapping[str, str] | None = None) -> RunSpec:
    return RunSpec(
        project_id, "img:dev", Path("/sec") / project_id, Path("/data"), env or {}, 720.0
    )


def test_run_args_mount_only_own_volume_and_secrets() -> None:
    args = run_args(spec())
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert mounts == [f"proj-{OWN}:/workspace", f"/sec/{OWN}:/run/secrets:ro", "/data:/seed:ro"]
    assert FOREIGN not in " ".join(args)


@pytest.mark.parametrize(
    "forbidden", ["--privileged", "--network", "--pid", "--ipc", "--cap-add", "--device"]
)
def test_run_args_have_no_escape_hatches(forbidden: str) -> None:
    args = run_args(spec())
    assert forbidden not in args
    assert not any("docker.sock" in a for a in args)
    for required in ("--read-only", "--cap-drop", "no-new-privileges"):
        assert required in args


def test_container_env_drops_secrets_and_foreign_keys() -> None:
    host = {
        "ANTHROPIC_AUTH_TOKEN": ANTHROPIC_TOKEN,
        "GATEWAY_AUTH_TOKEN": GATEWAY_TOKEN,
        "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_DEFAULT_SECRET_MODEL": "x",
        "ANTHROPIC_DEFAULT_OPUS_MODEL_KEY": "x",
        "AWS_SECRET_ACCESS_KEY": "x",
        "PROJECT_ID": FOREIGN,
        "HOME": "/Users/me",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus",
    }
    assert container_env(host) == {"ANTHROPIC_DEFAULT_OPUS_MODEL": "opus"}


# --- LEAK: журнал → события v0 ---------------------------------------------------------------

ROOT = "/workspace/project"


def journal_with_secrets(path: Path) -> list[Draft]:
    """Журнал пишется через боевой `EventLog` + `Redactor`, затем переводится `Translator`."""
    leak = f"ANTHROPIC_AUTH_TOKEN={ANTHROPIC_TOKEN} GATEWAY={GATEWAY_TOKEN}"
    secret_cmd = f"cat /run/secrets/anthropic_token; cat /workspace/.runner/events.jsonl; {leak}"
    with EventLog.open(path, Redactor.of(TOKENS), lambda: 1791273600.0) as log:
        log.append(EventKind.SESSION_STARTED, None, {"project_id": FOREIGN, "runner_version": "0"})
        log.append(EventKind.TURN_STARTED, "t1", {"prompt": f"секрет {ANTHROPIC_TOKEN}"})
        log.append(EventKind.SDK, "t1", {"type": "assistant", "parent_tool_use_id": None,
            "message": {"content": [
                {"type": "text", "text": f"токен {ANTHROPIC_TOKEN}, второй {GATEWAY_TOKEN}"},
                {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": secret_cmd}},
                {"type": "tool_use", "id": "r1", "name": "Agent",
                 "input": {"subagent_type": "writer", "description": f"роль {GATEWAY_TOKEN}"}},
                {"type": "tool_use", "id": "w1", "name": "Write",
                 "input": {"file_path": f"{ROOT}/docs/a.md", "content": leak}},
            ]}})  # fmt: skip
        log.append(EventKind.SDK, "t1", {"type": "user", "parent_tool_use_id": None,
            "message": {"content": [
                {"tool_use_id": "b1", "content": f"/proc/self/environ: {leak}", "is_error": False},
                {"tool_use_id": "r1", "content": "ok", "is_error": False},
                {"tool_use_id": "w1", "content": "ok", "is_error": False},
            ]}})  # fmt: skip
        log.append(
            EventKind.ACCESS_DENIED,
            "t1",
            {"tool": "Read", "path": "/run/secrets/anthropic_token", "reason": leak},
        )
        log.append(
            EventKind.FORK_QUESTION,
            "t1",
            {
                "fork_id": "f1",
                "questions": [
                    {"question": f"ключ {ANTHROPIC_TOKEN}?", "options": [{"label": GATEWAY_TOKEN}]}
                ],
            },
        )
        log.append(
            EventKind.FORK_ANSWERED,
            "t1",
            {"fork_id": "f1", "answers": {f"ключ {ANTHROPIC_TOKEN}?": GATEWAY_TOKEN}},
        )
        log.append(EventKind.CHECKPOINT, "t1", {"sha": "a" * 40})
        log.append(
            EventKind.TURN_FAILED,
            "t1",
            {"reason": "result_error", "detail": leak, "path": "/run/secrets/gateway_token"},
        )
    drafts = Translator(OWN, ROOT)
    return [d for e in read_journal(path.read_text(encoding="utf-8").splitlines())
            for d in drafts.feed(e)]  # fmt: skip


def public(drafts: list[Draft]) -> str:
    return json.dumps([(d.type.value, d.project_id, d.run_id, d.payload) for d in drafts],
                      ensure_ascii=False)  # fmt: skip


def test_public_events_carry_no_secrets_paths_or_foreign_project(tmp_path: Path) -> None:
    drafts = journal_with_secrets(tmp_path / "events.jsonl")
    text = public(drafts)
    assert len(drafts) >= 6  # события есть: проверка не пустая
    for token in TOKENS:
        assert token not in text
    for internal in ("/run/secrets", "/workspace/.runner", "/proc/", "ANTHROPIC_AUTH_TOKEN"):
        assert internal not in text
    assert {d.project_id for d in drafts} == {OWN}
    assert FOREIGN not in text


def test_journal_itself_masks_tokens(tmp_path: Path) -> None:
    journal_with_secrets(tmp_path / "events.jsonl")
    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert not any(token in raw for token in TOKENS)
    assert "***" in raw


@pytest.mark.parametrize(
    "written",
    [
        "/workspace/.runner/events.jsonl",
        "/workspace/claude/settings.json",
        "/workspace/project/../.runner/state.json",
        "/workspace/project-evil/a.md",
        "/workspace/project/.git/config",
        "//workspace/project/a.md",
        "/run/secrets/anthropic_token",
        "../.runner/state.json",
    ],
)
def test_artifact_path_outside_project_not_published(written: str) -> None:
    tr = Translator(OWN, ROOT)
    rows: list[tuple[int, EventKind, JsonValue]] = [
        (1, EventKind.TURN_STARTED, {}),
        (2, EventKind.SDK, {"type": "assistant", "parent_tool_use_id": None, "message": {
            "content": [{"type": "tool_use", "id": "w", "name": "Write",
                         "input": {"file_path": written}}]}}),
        (3, EventKind.SDK, {"type": "user", "parent_tool_use_id": None, "message": {
            "content": [{"tool_use_id": "w", "content": "ok"}]}}),
        (4, EventKind.CHECKPOINT, {"sha": "b" * 40}),
    ]  # fmt: skip
    drafts = [d for seq, kind, payload in rows
              for d in tr.feed(Event(seq, 1.0, "t1", kind, payload))]  # fmt: skip
    assert [d.type.value for d in drafts] == ["run.started"]


@pytest.mark.xfail(
    strict=True,
    reason="v0.py (_assistant_block): авторский текст модели публикуется как есть, внутренние "
    "пути не вырезаются; предложение: Translator маскирует /run/secrets и /workspace/.runner, "
    "корень проекта сводит к относительному пути",
)
@pytest.mark.parametrize(
    "secret_path", ["/run/secrets/anthropic_token", "/workspace/.runner/state.json"]
)
def test_internal_path_in_model_text_not_published(secret_path: str) -> None:
    tr = Translator(OWN, ROOT)
    tr.feed(Event(1, 1.0, "t1", EventKind.TURN_STARTED, {}))
    message: dict[str, JsonValue] = {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {"content": [{"type": "text", "text": f"читаю {secret_path}"}]},
    }
    drafts = tr.feed(Event(2, 1.0, "t1", EventKind.SDK, message))
    assert secret_path not in public(list(drafts))


@pytest.mark.xfail(
    strict=True,
    reason="redact.py (Redactor.text): только дословное совпадение, закодированный токен "
    "(base64, percent-encoding) проходит; нужна маскировка типовых кодировок секрета",
)
@pytest.mark.parametrize(
    "encode",
    [lambda t: base64.b64encode(t.encode()).decode(), lambda t: t.encode().hex()],
    ids=["base64", "hex"],
)
def test_encoded_token_masked(encode: Callable[[str], str]) -> None:
    redactor = Redactor.of(TOKENS)
    assert encode(ANTHROPIC_TOKEN) not in str(redactor.apply({"t": encode(ANTHROPIC_TOKEN)}))
