# 2.4 Runner — этап 1 (демо 28.09): план реализации

> **Для исполнителя:** выполнять по задачам через `executing-plans`. Дизайн: `docs/plans/2026-09-25-runner-design.md`.

**Цель:** контейнер проекта с Claude Code + CTF стартует с данными проекта, пишет журнал событий, отличает «работает / зависла / упала» и отклоняет с записью в журнал доступ за пределы проекта.

**Архитектура:** пакет `ctrunner` в `runner/`. Чистое ядро (`redact`, `liveness`, `guard`, `turn`, `sdkevents`, `protocol`) и императивная оболочка (`main`, `inbox`, `healthcheck`, `sessions`). Runner держит один долгоживущий `ClaudeSDKClient` и одного читателя `receive_messages()`; каждое сообщение проходит через чистую машину состояний хода `turn.step`. Команды без гейтвея приходят через файловый inbox на томе (`sessions send`); на этапе 2 тот же тип `Command` приходит по WebSocket.

**Стек:** Python 3.12, uv 0.9, `claude-agent-sdk==0.2.158` (зафиксировано в песочнице), pytest + pytest-asyncio, ruff, mypy `--strict`, Docker 28.

**Ограничения:**
- Работа в ветке `romanov` репозитория `AcryCxde/PP-Sber-Autumn-2026`; в `master` напрямую не пушим. Коммиты — только по запросу пользователя.
- Ключ cliproxy не попадает в репозиторий, образ, `docker inspect`, журнал и вывод.
- Предупреждения запрещены: ruff и mypy без ошибок, pytest с `-W error`.
- Все команды выполняются из `runner/`, если не сказано иначе.

**Не входит в этап 1 (идёт в план этапа 2):** `state.json`, checkpoint/git commit, resume и автопродолжение, `eval_restart`, WS-клиент, заглушка гейтвея, повтор событий, события `checkpoint`/`turn_interrupted`/`turn_resumed`, `command_ack`. Также вне этапа: несколько сессий, авто-убийство зависшей сессии, egress-фильтр.

**Отличия от дизайна (уточнения при планировании):**
- `Phase` получает четвёртое значение `failed` — SDK завершился с ошибкой; `assess(failed) → crashed`. Отразить в `protocol.md`.
- Пока нет гейтвея, команды передаются через `/workspace/.runner/inbox/` (`sessions send` → `docker exec`).
- Данные проекта монтируются только на чтение в `/seed`; при первом старте runner копирует их в `/workspace/project`.
- `HOME` находится на tmpfs (`--read-only` rootfs), git-идентичность задаётся через `GIT_*` env.

---

### Task 0: Каркас проекта `runner/`

**Files:**
- Create: `runner/pyproject.toml`, `runner/src/ctrunner/__init__.py`, `runner/tests/test_smoke.py`, `runner/.python-version`, `.gitignore` (корень репозитория)

**Step 1: Тест**

```python
# runner/tests/test_smoke.py
import ctrunner


def test_version_is_set() -> None:
    assert ctrunner.__version__ == "0.1.0"
```

**Step 2: RED**

Run: `cd runner && uv run pytest -q` (до создания `__init__.py`)
Expected: FAIL `ModuleNotFoundError: No module named 'ctrunner'`

**Step 3: Реализация**

```toml
# runner/pyproject.toml
[project]
name = "ctrunner"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = ["claude-agent-sdk==0.2.158"]

[project.scripts]
ctrunner = "ctrunner.main:cli"
ctrunner-healthcheck = "ctrunner.healthcheck:cli"
ctrunner-inbox = "ctrunner.inbox:cli"
sessions = "ctrunner.sessions:cli"

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "ruff>=0.6", "mypy>=1.11"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ctrunner"]

[tool.pytest.ini_options]
addopts = "-W error --strict-markers"
asyncio_mode = "auto"
markers = ["e2e: нужны Docker и cliproxy"]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "SIM", "RUF", "S", "ASYNC", "PL"]
ignore = ["PLR2004"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "PLR2004"]
"evals/**" = ["S101", "S603", "S607"]

[tool.mypy]
strict = true
warn_unreachable = true
```

`runner/src/ctrunner/__init__.py`: `__version__ = "0.1.0"`. `.python-version`: `3.12`. В корневой `.gitignore`: `.idea/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`.

**Step 4: GREEN**

Run: `uv sync && uv run pytest -q`
Expected: `1 passed`

**Step 5: Gates**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`
Expected: без ошибок

---

### Task 1: `protocol.py` — общие типы JSON, команд и событий

**Files:**
- Create: `runner/src/ctrunner/protocol.py`
- Test: `runner/tests/test_protocol.py`

**Step 1: Тест**

```python
import pytest

from ctrunner.protocol import (
    ForkAnswerCmd, MessageCmd, ProtocolError, StopCmd, parse_command,
)


def test_parse_message() -> None:
    assert parse_command({"id": "c1", "kind": "message", "text": "привет"}) == MessageCmd("c1", "привет")


def test_parse_fork_answer() -> None:
    raw = {"id": "c2", "kind": "fork_answer", "fork_id": "f1", "answers": {"Вопрос?": "Да"}}
    assert parse_command(raw) == ForkAnswerCmd("c2", "f1", {"Вопрос?": "Да"})


def test_parse_stop() -> None:
    assert parse_command({"id": "c3", "kind": "stop"}) == StopCmd("c3")


@pytest.mark.parametrize("raw", [
    {}, {"id": "c", "kind": "nope"}, {"id": "", "kind": "stop"},
    {"id": "c", "kind": "message"}, {"id": "c", "kind": "message", "text": ""},
    {"id": "c", "kind": "fork_answer", "fork_id": "f", "answers": {"q": 1}},
    ["not", "object"],
])
def test_rejects_malformed(raw: object) -> None:
    with pytest.raises(ProtocolError):
        parse_command(raw)
```

**Step 2: RED**

Run: `uv run pytest tests/test_protocol.py -q`
Expected: FAIL `ModuleNotFoundError: ctrunner.protocol`

**Step 3: Реализация**

```python
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

class ProtocolError(ValueError): ...

@dataclass(frozen=True, slots=True)
class MessageCmd:     id: str; text: str
@dataclass(frozen=True, slots=True)
class ForkAnswerCmd:  id: str; fork_id: str; answers: dict[str, str]
@dataclass(frozen=True, slots=True)
class StopCmd:        id: str

type Command = MessageCmd | ForkAnswerCmd | StopCmd

class EventKind(StrEnum):
    SESSION_STARTED = "session_started"; TURN_STARTED = "turn_started"; SDK = "sdk"
    FORK_QUESTION = "fork_question"; FORK_ANSWERED = "fork_answered"
    TURN_COMPLETED = "turn_completed"; TURN_FAILED = "turn_failed"
    ACCESS_DENIED = "access_denied"

def parse_command(raw: object) -> Command
```

`parse_command`: `raw` должен быть `dict`; `id` — непустая `str`; ветвление `match raw.get("kind")` по трём литералам, иначе `ProtocolError(f"unknown kind: {kind!r}")`. Для `message` поле `text` — непустая `str`. Для `fork_answer` `answers` — `dict[str, str]` (все ключи и значения `str`).

**Step 4: GREEN** — `uv run pytest tests/test_protocol.py -q` → PASS

**Step 5: Gates** — `uv run ruff check . && uv run mypy src tests` → без ошибок

---

### Task 2: `redact.py` — вырезание значений секретов

**Files:**
- Create: `runner/src/ctrunner/redact.py`
- Test: `runner/tests/test_redact.py`

**Step 1: Тест**

```python
import pytest

from ctrunner.redact import Redactor


def test_redacts_nested_strings() -> None:
    r = Redactor.of(["sk-SECRET-123"])
    value = {"a": ["x sk-SECRET-123 y", {"b": "sk-SECRET-123"}], "n": 5, "z": None}
    assert r.apply(value) == {"a": ["x *** y", {"b": "***"}], "n": 5, "z": None}


def test_redacts_dict_keys() -> None:
    assert Redactor.of(["k3y"]).apply({"k3y": 1}) == {"***": 1}


def test_longest_secret_first() -> None:
    assert Redactor.of(["abc", "abcdef"]).apply("abcdef") == "***"


@pytest.mark.parametrize("short", ["", "short"])
def test_rejects_short_secrets(short: str) -> None:
    with pytest.raises(ValueError, match="secret too short"):
        Redactor.of([short])
```

**Step 2: RED** — `uv run pytest tests/test_redact.py -q` → FAIL (нет модуля)

**Step 3: Реализация**

```python
MIN_SECRET_LEN = 8  # короче — ложные совпадения испортят вывод

@dataclass(frozen=True, slots=True)
class Redactor:
    secrets: tuple[str, ...]  # отсортированы по убыванию длины

    @classmethod
    def of(cls, secrets: Iterable[str]) -> Redactor: ...  # ValueError("secret too short") при len < MIN_SECRET_LEN
    def apply(self, value: JsonValue) -> JsonValue: ...    # рекурсивно по str, list, dict (ключи тоже)
```

**Step 4: GREEN** — PASS
**Step 5: Gates** — ruff + mypy без ошибок

---

### Task 3: `liveness.py` — правило живости

**Files:**
- Create: `runner/src/ctrunner/liveness.py`
- Test: `runner/tests/test_liveness.py`

**Step 1: Тест**

```python
import pytest

from ctrunner.liveness import Health, Phase, assess

T = 720.0


@pytest.mark.parametrize(("phase", "idle_for", "expected"), [
    (Phase.WORKING, 0.0, Health.OK),
    (Phase.WORKING, T, Health.OK),               # граница: ровно T — ещё ок
    (Phase.WORKING, T + 0.1, Health.STALLED),
    (Phase.IDLE, 10 * T, Health.OK),             # простой без хода — не зависание
    (Phase.AWAITING_ANSWER, 10 * T, Health.OK),  # человек думает — не зависание
    (Phase.FAILED, 0.0, Health.CRASHED),
])
def test_assess(phase: Phase, idle_for: float, expected: Health) -> None:
    assert assess(phase, last_progress_at=1000.0, now=1000.0 + idle_for, stall_after_s=T) is expected
```

**Step 2: RED** — FAIL (нет модуля)

**Step 3: Реализация**

```python
class Phase(StrEnum):  IDLE="idle"; WORKING="working"; AWAITING_ANSWER="awaiting_answer"; FAILED="failed"
class Health(StrEnum): OK="ok"; STALLED="stalled"; CRASHED="crashed"

def assess(phase: Phase, *, last_progress_at: float, now: float, stall_after_s: float) -> Health:
    match phase:
        case Phase.WORKING:
            return Health.STALLED if now - last_progress_at > stall_after_s else Health.OK
        case Phase.IDLE | Phase.AWAITING_ANSWER:
            return Health.OK
        case Phase.FAILED:
            return Health.CRASHED
```

Время монотонное (`time.monotonic()`), передаётся снаружи.

**Step 4: GREEN** — PASS
**Step 5: Gates** — ruff + mypy без ошибок

---

### Task 4: `guard.py` — граница рабочей директории

**Files:**
- Create: `runner/src/ctrunner/guard.py`
- Test: `runner/tests/test_guard.py`

**Step 1: Тест**

```python
from pathlib import Path

import pytest

from ctrunner.guard import Allow, Deny, Policy, check

PROJECT = Path("/workspace/project")
POLICY = Policy(roots=(PROJECT, Path("/tmp")),
                bash_system=(Path("/usr"), Path("/bin"), Path("/lib"), Path("/dev/null")))


def ident(p: Path) -> Path:
    return p


def symlink_escape(p: Path) -> Path:
    return Path("/workspace/other") if p == PROJECT / "link" else p


@pytest.mark.parametrize(("tool", "tool_input"), [
    ("Read", {"file_path": "/workspace/project/a.md"}),
    ("Read", {"file_path": "notes/a.md"}),
    ("Write", {"file_path": "/tmp/x"}),
    ("Glob", {"pattern": "**/*.py"}),
    ("Grep", {"pattern": "x", "path": "src"}),
    ("Bash", {"command": "ls -la && git status"}),
    ("Bash", {"command": "/usr/bin/env python -V > /dev/null"}),
    ("Agent", {"prompt": "read /etc/passwd"}),  # не файловый инструмент
])
def test_allows(tool: str, tool_input: dict[str, object]) -> None:
    assert check(tool, tool_input, PROJECT, POLICY, ident) == Allow()


@pytest.mark.parametrize(("tool", "tool_input", "path"), [
    ("Read", {"file_path": "/workspace/proj-other/secret.md"}, "/workspace/proj-other/secret.md"),
    ("Read", {"file_path": "../proj-other/a"}, "/workspace/proj-other/a"),
    ("Read", {"file_path": "/workspace/.runner/events.jsonl"}, "/workspace/.runner/events.jsonl"),
    ("Edit", {"file_path": "/etc/passwd"}, "/etc/passwd"),
    ("Glob", {"pattern": "/workspace/claude/**"}, "/workspace/claude"),
    ("Grep", {"pattern": "k", "path": "/run/secrets"}, "/run/secrets"),
    ("Read", {"file_path": "link/x"}, "/workspace/other/x"),
    ("Bash", {"command": "cat ../proj-other/a"}, "/workspace/proj-other/a"),
    ("Bash", {"command": "cat /etc/passwd"}, "/etc/passwd"),
    ("Bash", {"command": "cp x --target=/workspace/claude"}, "/workspace/claude"),
])
def test_denies(tool: str, tool_input: dict[str, object], path: str) -> None:
    resolve = symlink_escape if "link" in str(tool_input) else ident
    verdict = check(tool, tool_input, PROJECT, POLICY, resolve)
    assert isinstance(verdict, Deny)
    assert verdict.path == path


def test_bash_unbalanced_quotes_falls_back() -> None:
    assert isinstance(check("Bash", {"command": "cat '/etc/passwd"}, PROJECT, POLICY, ident), Deny)
```

**Step 2: RED** — FAIL (нет модуля)

**Step 3: Реализация**

```python
@dataclass(frozen=True, slots=True)
class Allow: ...
@dataclass(frozen=True, slots=True)
class Deny: path: str; reason: str
type Verdict = Allow | Deny

@dataclass(frozen=True, slots=True)
class Policy:
    roots: tuple[Path, ...]        # файловые инструменты и Bash
    bash_system: tuple[Path, ...]  # дополнительно разрешено только в Bash

PATH_FIELDS: Final[Mapping[str, str]] = {
    "Read": "file_path", "Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path", "Glob": "path", "Grep": "path", "LS": "path",
}
GLOB_CHARS: Final = frozenset("*?[{")

def check(tool: str, tool_input: Mapping[str, object], cwd: Path,
          policy: Policy, resolve: Callable[[Path], Path]) -> Verdict
```

Логика:
- `normalize(raw, cwd)` = `Path(os.path.normpath(cwd / Path(raw).expanduser()))`, затем `resolve(...)`. В проде `resolve = lambda p: Path(os.path.realpath(p))` — symlink-и раскрываются.
- Путь внутри корня: `p == root or p.is_relative_to(root)`.
- Файловый инструмент: проверяется поле из `PATH_FIELDS`, если это `str`. Для `Glob` дополнительно `pattern`, если он абсолютный: берутся компоненты до первого, содержащего `GLOB_CHARS`.
- `Bash`: `shlex.split(command)`, при `ValueError` — `command.split()`. У каждого токена отрезаются ведущие `<>|&;()`, берётся часть после последнего `=`. Кандидат — токен, начинающийся с `/` или `~` либо содержащий `..`. Разрешён, если внутри `roots + bash_system`.
- Остальные инструменты → `Allow()`.
- `Deny.reason` = `"доступ за пределами проекта запрещён: <path>"`.

Эвристика Bash **не является границей безопасности**: граница — монтирование. Это сказать в docstring модуля.

**Step 4: GREEN** — PASS
**Step 5: Gates** — ruff + mypy без ошибок

---

### Task 5: `fsio.py` + `eventlog.py` — атомарная запись и журнал событий

**Files:**
- Create: `runner/src/ctrunner/fsio.py`, `runner/src/ctrunner/eventlog.py`
- Test: `runner/tests/test_eventlog.py`

**Step 1: Тест**

```python
import json
from pathlib import Path

import pytest

from ctrunner.eventlog import EventLog
from ctrunner.fsio import write_json_atomic
from ctrunner.protocol import EventKind
from ctrunner.redact import Redactor

R = Redactor.of(["SECRET-TOKEN-1"])


def lines(p: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in p.read_text().splitlines()]


def test_seq_is_monotonic_across_reopen(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    log = EventLog.open(p, R, clock=lambda: 1.0)
    log.append(EventKind.SESSION_STARTED, None, {})
    log.close()
    log = EventLog.open(p, R, clock=lambda: 2.0)
    e = log.append(EventKind.TURN_STARTED, "t1", {})
    assert e.seq == 2
    assert [x["seq"] for x in lines(p)] == [1, 2]


def test_payload_is_redacted_on_disk(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    EventLog.open(p, R, clock=lambda: 1.0).append(EventKind.SDK, "t1", {"text": "key SECRET-TOKEN-1"})
    assert "SECRET-TOKEN-1" not in p.read_text()


def test_torn_last_line_is_truncated(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_text('{"seq": 1, "ts": 1, "turn_id": null, "kind": "sdk", "payload": {}}\n{"seq": 2, "ki')
    log = EventLog.open(p, R, clock=lambda: 1.0)
    assert log.append(EventKind.SDK, None, {}).seq == 2
    assert [x["seq"] for x in lines(p)] == [1, 2]


def test_atomic_write_keeps_old_file_on_failure(tmp_path: Path) -> None:
    p = tmp_path / "h.json"
    write_json_atomic(p, {"v": 1})
    with pytest.raises(TypeError):
        write_json_atomic(p, {"v": object()})  # type: ignore[dict-item]
    assert json.loads(p.read_text()) == {"v": 1}
    assert list(tmp_path.iterdir()) == [p]
```

**Step 2: RED** — FAIL (нет модулей)

**Step 3: Реализация**

```python
# fsio.py
def write_json_atomic(path: Path, value: JsonValue) -> None:
    # сериализация до открытия файла; tmp в той же директории; fsync(tmp) → os.replace → fsync(dir)
    # при исключении tmp удаляется
    ...

# eventlog.py
@dataclass(frozen=True, slots=True)
class Event:
    seq: int; ts: float; turn_id: str | None; kind: EventKind; payload: JsonValue
    def to_json(self) -> dict[str, JsonValue]: ...

class EventLog:
    @classmethod
    def open(cls, path: Path, redactor: Redactor, clock: Callable[[], float]) -> EventLog: ...
    def append(self, kind: EventKind, turn_id: str | None, payload: JsonValue) -> Event: ...
    def close(self) -> None: ...
```

`open`: читает файл, отбрасывает хвост после последнего `\n`, если он не парсится как JSON (`os.truncate`); `seq` = `seq` последней строки или 0; файл открывается в `"a"`. `append`: `payload = redactor.apply(payload)`, одна строка `json.dumps(..., ensure_ascii=False)`, затем `flush` + `os.fsync`. `clock` — `time.time` (время на стене, для людей). ENOSPC не перехватывается: `OSError` уходит в оболочку (Task 10).

**Step 4: GREEN** — PASS
**Step 5: Gates** — ruff + mypy без ошибок

---

### Task 6: `config.py` — конфигурация и секреты, fail-fast

**Files:**
- Create: `runner/src/ctrunner/config.py`
- Test: `runner/tests/test_config.py`

**Step 1: Тест**

```python
from pathlib import Path

import pytest

from ctrunner.config import ConfigError, load_config

ENV = {
    "PROJECT_ID": "demo-a",
    "ANTHROPIC_BASE_URL": "http://proxy:8317",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-5.6-sol",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-5.6-terra",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-5.6-luna",
}


@pytest.fixture
def secrets(tmp_path: Path) -> Path:
    (tmp_path / "anthropic_token").write_text("sk-test-0123456789\n")
    return tmp_path


def test_loads(secrets: Path) -> None:
    c = load_config(ENV, secrets)
    assert c.project_id == "demo-a"
    assert c.anthropic_token == "sk-test-0123456789"
    assert c.stall_after_s == 720.0
    assert c.workspace == Path("/workspace")


def test_repr_hides_token(secrets: Path) -> None:
    assert "sk-test" not in repr(load_config(ENV, secrets))


def test_missing_secret(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="anthropic_token"):
        load_config(ENV, tmp_path)


@pytest.mark.parametrize(("key", "value"), [
    ("PROJECT_ID", "../x"), ("PROJECT_ID", ""), ("STALL_AFTER_S", "0"), ("STALL_AFTER_S", "abc"),
    ("ANTHROPIC_BASE_URL", "ftp://x"),
])
def test_invalid_env(secrets: Path, key: str, value: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config({**ENV, key: value}, secrets)


def test_missing_model_env(secrets: Path) -> None:
    env = {k: v for k, v in ENV.items() if k != "ANTHROPIC_DEFAULT_HAIKU_MODEL"}
    with pytest.raises(ConfigError, match="ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        load_config(env, secrets)
```

**Step 2: RED** — FAIL (нет модуля)

**Step 3: Реализация**

```python
PROJECT_ID_RE: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
MODEL_ENV: Final = ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL")

class ConfigError(Exception): ...

@dataclass(frozen=True, slots=True)
class RunnerConfig:
    project_id: str
    anthropic_base_url: str
    anthropic_token: str = field(repr=False)
    models: Mapping[str, str]            # MODEL_ENV → значение; + ANTHROPIC_DEFAULT_FABLE_MODEL, если задан
    stall_after_s: float
    workspace: Path

    @property
    def project_dir(self) -> Path: return self.workspace / "project"
    @property
    def runner_dir(self) -> Path: return self.workspace / ".runner"
    @property
    def claude_dir(self) -> Path: return self.workspace / "claude"
    def sdk_env(self) -> dict[str, str]: ...
    # ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, модели, CLAUDE_CONFIG_DIR=str(claude_dir)

def load_config(env: Mapping[str, str], secrets_dir: Path) -> RunnerConfig
```

`PROJECT_ID` проверяется через `fullmatch`. URL — схема `http` или `https`. `STALL_AFTER_S` — `float > 0`, по умолчанию 720. `WORKSPACE`, по умолчанию `/workspace`. Токен: `(secrets_dir / "anthropic_token").read_text().strip()`; если его нет или он пустой — `ConfigError("secret anthropic_token missing")`. Текст ошибки называет ключ и никогда не содержит значения.

**Step 4: GREEN** — PASS
**Step 5: Gates** — ruff + mypy без ошибок

---

### Task 7: `probe.py` — проверка ключа cliproxy

**Files:**
- Create: `runner/src/ctrunner/probe.py`
- Test: `runner/tests/test_probe.py`

**Step 1: Тест** — локальный `http.server` в потоке

```python
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ctrunner.probe import ProbeResult, probe


def serve(code: int) -> tuple[HTTPServer, str]:
    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["content-length"]))
            ok = self.headers["authorization"] == "Bearer sk-good-0000"
            self.send_response(code if ok else 401)
            self.end_headers()
            self.wfile.write(b"{}")
        def log_message(self, *a: object) -> None: ...
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


@pytest.fixture
def ok_url() -> Iterator[str]:
    srv, url = serve(200); yield url; srv.shutdown()


def test_ok(ok_url: str) -> None:
    assert probe(ok_url, "sk-good-0000", "m", timeout_s=5) is ProbeResult.OK


def test_rejected(ok_url: str) -> None:
    assert probe(ok_url, "sk-bad-00000", "m", timeout_s=5) is ProbeResult.REJECTED


def test_unreachable() -> None:
    assert probe("http://127.0.0.1:9", "sk-good-0000", "m", timeout_s=1) is ProbeResult.UNREACHABLE


def test_upstream_error() -> None:
    srv, url = serve(503)
    try:
        assert probe(url, "sk-good-0000", "m", timeout_s=5) is ProbeResult.UNREACHABLE
    finally:
        srv.shutdown()
```

**Step 2: RED** — FAIL (нет модуля)

**Step 3: Реализация**

```python
class ProbeResult(StrEnum): OK="ok"; REJECTED="rejected"; UNREACHABLE="unreachable"

def probe(base_url: str, token: str, model: str, *, timeout_s: float) -> ProbeResult
```

`urllib.request` (stdlib), `POST {base_url}/v1/messages`, заголовки как в `0-proxy.sh` песочницы (`Authorization: Bearer`, `anthropic-version: 2023-06-01`), тело `{"model", "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]}`. Результат: 2xx → OK; `HTTPError` с 401/403 → REJECTED; прочий `HTTPError`, `URLError`, `TimeoutError` → UNREACHABLE. Тело ответа и токен не логируются. Модель — `ANTHROPIC_DEFAULT_HAIKU_MODEL`.

**Step 4: GREEN** — PASS
**Step 5: Gates** — ruff (`S310` для urllib: схема проверена в config, подавить точечно с комментарием) + mypy без ошибок

---

### Task 8: `sdkevents.py` — нормализация сообщений SDK

**Files:**
- Create: `runner/src/ctrunner/sdkevents.py`
- Test: `runner/tests/test_sdkevents.py`

Перенос `normalize` из `coreteams-sandbox/lib/fork.py:28-42` с типами.

**Step 1: Тест**

```python
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, SystemMessage, TextBlock

from ctrunner.sdkevents import normalize, session_id_of


def result(is_error: bool = False) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id="s1")


def test_assistant() -> None:
    m = AssistantMessage(content=[TextBlock(text="hi")], model="m", parent_tool_use_id="tu1")
    assert normalize(m) == {"type": "assistant", "parent_tool_use_id": "tu1",
                            "message": {"model": "m", "content": [{"text": "hi"}]}}


def test_system_passes_data() -> None:
    m = SystemMessage(subtype="task_started", data={"type": "system", "subtype": "task_started", "task_id": "a"})
    assert normalize(m) == m.data


def test_result() -> None:
    n = normalize(result())
    assert n["type"] == "result" and n["session_id"] == "s1"


def test_stream_event_is_not_normalized() -> None:
    assert normalize(StreamEvent(uuid="u", session_id="s1", event={})) is None


def test_session_id() -> None:
    assert session_id_of(result()) == "s1"
```

**Step 2: RED** — FAIL (нет модуля)

**Step 3: Реализация**

```python
def normalize(msg: Message) -> dict[str, JsonValue] | None
def session_id_of(msg: Message) -> str | None
```

`match msg` по классам `UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent | RateLimitEvent | ConversationResetMessage` (все из `claude_agent_sdk.types.Message`). Для `StreamEvent` возвращается `None`: наружу не уходит. Значения превращаются в JSON через `json.loads(json.dumps(dataclasses.asdict(...), default=str))` — один раз на границе.

Перед реализацией проверить, что `RateLimitEvent` и `ConversationResetMessage` экспортируются из `claude_agent_sdk`: `uv run python -c "from claude_agent_sdk import RateLimitEvent, ConversationResetMessage"`. Если нет — импортировать из `claude_agent_sdk.types`.

**Step 4: GREEN** — PASS
**Step 5: Gates** — ruff + mypy без ошибок

---

### Task 9: `turn.py` — машина состояний хода (чистая)

**Files:**
- Create: `runner/src/ctrunner/turn.py`
- Test: `runner/tests/test_turn.py`

**Step 1: Тест**

```python
from claude_agent_sdk import ResultMessage, SystemMessage

from ctrunner.liveness import Phase
from ctrunner.protocol import MessageCmd
from ctrunner.turn import (
    Idle, SendPrompt, TurnDone, TurnFailedFx, Working, on_command, on_fork_closed, on_fork_open, on_message,
)


def res(is_error: bool = False) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id="s1")


def sysmsg(subtype: str, **data: object) -> SystemMessage:
    return SystemMessage(subtype=subtype, data={"type": "system", "subtype": subtype, **data})


def test_idle_command_starts_turn() -> None:
    state, fx = on_command(Idle(queue=()), MessageCmd("c1", "go"), turn_id="t1")
    assert state == Working(turn_id="t1", pending=frozenset(), forks=0, queue=())
    assert fx == (SendPrompt("t1", "go"),)


def test_command_while_working_is_queued() -> None:
    w = Working("t1", frozenset(), 0, ())
    state, fx = on_command(w, MessageCmd("c2", "next"), turn_id="t2")
    assert state == Working("t1", frozenset(), 0, (MessageCmd("c2", "next"),)) and fx == ()


def test_result_with_background_task_does_not_end_turn() -> None:
    s = Working("t1", frozenset(), 0, ())
    s, _ = on_message(s, sysmsg("task_started", task_id="a", is_backgrounded=True))
    s, fx = on_message(s, res())
    assert isinstance(s, Working) and fx == ()
    s, _ = on_message(s, sysmsg("task_notification", task_id="a"))
    s, fx = on_message(s, res())
    assert s == Idle(queue=()) and fx == (TurnDone("t1"),)


def test_error_result_fails_turn() -> None:
    s, fx = on_message(Working("t1", frozenset(), 0, ()), res(is_error=True))
    assert s == Idle(queue=()) and fx == (TurnFailedFx("t1", "result_error"),)


def test_queued_command_starts_after_done() -> None:
    s, fx = on_message(Working("t1", frozenset(), 0, (MessageCmd("c2", "next"),)), res())
    assert s == Idle(queue=(MessageCmd("c2", "next"),)) and fx == (TurnDone("t1"),)


def test_phase() -> None:
    w = Working("t1", frozenset(), 0, ())
    assert w.phase is Phase.WORKING
    assert on_fork_open(w).phase is Phase.AWAITING_ANSWER
    assert on_fork_closed(on_fork_open(w)).phase is Phase.WORKING
    assert Idle(queue=()).phase is Phase.IDLE
```

**Step 2: RED** — FAIL (нет модуля)

**Step 3: Реализация**

```python
@dataclass(frozen=True, slots=True)
class Idle:
    queue: tuple[MessageCmd, ...]
    @property
    def phase(self) -> Phase: return Phase.IDLE

@dataclass(frozen=True, slots=True)
class Working:
    turn_id: str; pending: frozenset[str]; forks: int; queue: tuple[MessageCmd, ...]
    @property
    def phase(self) -> Phase: return Phase.AWAITING_ANSWER if self.forks else Phase.WORKING

type TurnState = Idle | Working

@dataclass(frozen=True, slots=True)
class SendPrompt:    turn_id: str; text: str
@dataclass(frozen=True, slots=True)
class TurnDone:      turn_id: str
@dataclass(frozen=True, slots=True)
class TurnFailedFx:  turn_id: str; reason: str
type Effect = SendPrompt | TurnDone | TurnFailedFx

def on_command(s: TurnState, cmd: MessageCmd, *, turn_id: str) -> tuple[TurnState, tuple[Effect, ...]]
def on_message(s: TurnState, msg: Message) -> tuple[TurnState, tuple[Effect, ...]]
def on_fork_open(s: Working) -> Working
def on_fork_closed(s: Working) -> Working
def next_queued(s: Idle, *, turn_id: str) -> tuple[TurnState, tuple[Effect, ...]]
```

Учёт фоновых задач — как в `fork.py:84-93` песочницы: `task_started` с `is_backgrounded in (True, "True")` → add; `task_notification` → discard; `background_tasks_changed` со списком `tasks` → заменить множество. `ResultMessage` в `Idle` игнорируется: это результат фоновой доработки после хода, его событие уже записал reader.

**Step 4: GREEN** — PASS
**Step 5: Gates** — ruff + mypy без ошибок

---

### Task 10: `inbox.py` + `main.py` — императивная оболочка runner

**Files:**
- Create: `runner/src/ctrunner/inbox.py`, `runner/src/ctrunner/health.py`, `runner/src/ctrunner/main.py`
- Test: `runner/tests/test_inbox.py`, `runner/tests/test_main_exit.py`

**Step 1: Тесты**

```python
# test_inbox.py
from pathlib import Path

from ctrunner.inbox import Inbox, put
from ctrunner.protocol import MessageCmd


def test_put_then_take(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "go"})
    box = Inbox(tmp_path)
    assert box.take() == [MessageCmd("c1", "go")]
    assert box.take() == []  # файл перенесён в processed/


def test_malformed_goes_to_rejected(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "bogus"})
    assert Inbox(tmp_path).take() == []
    assert len(list((tmp_path / "rejected").iterdir())) == 1


def test_duplicate_id_ignored(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    box = Inbox(tmp_path); box.take()
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    assert box.take() == []
```

```python
# test_main_exit.py
from pathlib import Path

from ctrunner.main import EX_CONFIG, preflight


def test_missing_secret_exits_78_before_anything(tmp_path: Path) -> None:
    code = preflight({"PROJECT_ID": "demo-a"}, secrets_dir=tmp_path, probe_fn=lambda *a, **k: None)
    assert code == EX_CONFIG
    assert not (tmp_path / "events.jsonl").exists()
```

**Step 2: RED** — FAIL (нет модулей)

**Step 3: Реализация**

`inbox.py`:
- `put(dir, raw)`: запись в `dir/.tmp-<uuid>`, затем `os.replace` в `dir/<ns>-<uuid>.json` — читатель никогда не видит половину файла.
- `Inbox.take() -> list[Command]`: читает `*.json` по имени файла, разбирает через `parse_command`. Успешные переносит в `processed/`, ошибки — в `rejected/`. Дубли по `id` отсекает по множеству, восстановленному из `processed/` при создании `Inbox`.
- `cli()`: `ctrunner-inbox message "текст"` | `ctrunner-inbox fork-answer <fork_id> <label>` | `ctrunner-inbox stop`. Каталог — `$WORKSPACE/.runner/inbox`.

`health.py`:
- `write_health(path, phase, health, last_progress_wall)` → `write_json_atomic(path, {"phase", "health", "last_progress_at", "written_at": time.time()})`.

`main.py` (оболочка; ветвление только здесь):
```python
EX_CONFIG: Final = 78

def preflight(env, *, secrets_dir, probe_fn) -> RunnerConfig | int
    # ConfigError → stderr: только имя ключа → EX_CONFIG
    # probe REJECTED → EX_CONFIG; UNREACHABLE → 1 (Docker перезапустит)

async def run(cfg: RunnerConfig) -> int
def cli() -> None:  # sys.exit(...)
```

`run`:
1. `seed_project(cfg)`: если нет `project_dir` — `shutil.copytree("/seed", project_dir)`, если `/seed` существует, иначе `mkdir`. Если нет `project_dir/.claude` — копия `/opt/ctf/.claude`. Если нет `.git` — `git init -q` + `git add -A` + `git commit -qm init` через `subprocess.run([...], check=True)`.
2. `EventLog.open(runner_dir/"events.jsonl", Redactor.of([cfg.anthropic_token]), time.time)` и событие `session_started{project_id, runner_version}`.
3. `ClaudeAgentOptions(cwd=project_dir, setting_sources=["project"], env=cfg.sdk_env(), include_partial_messages=True, hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]}, can_use_tool=can_use_tool, stderr=<строки в stderr после Redactor>)`.
4. Три задачи в `asyncio.TaskGroup`:
   - **reader**: `async for msg in client.receive_messages()` → `progress = time.monotonic()`; если `normalize(msg)` не `None` — `log.append(SDK, turn_id, ...)`; `state, fx = on_message(state, msg)`; применить эффекты.
   - **commands**: раз в 1 с `inbox.take()`. `MessageCmd` → `on_command` → `SendPrompt` → `turn_started` + `await client.query(text)`. `ForkAnswerCmd` → разрешить `asyncio.Future` из `forks[fork_id]`. `StopCmd` → отменить группу, выход 0.
   - **heartbeat**: раз в 10 с `write_health(/tmp/health.json, state.phase, assess(...), ...)`.
5. `pre_tool_use`: `guard.check(...)` с `Policy(roots=(project_dir, Path("/tmp")), bash_system=(/usr, /bin, /lib, /dev/null))` и `resolve=realpath`. На `Deny` — `log.append(ACCESS_DENIED, turn_id, {tool, path, agent_id})` и возврат `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}`.
6. `can_use_tool`: не `AskUserQuestion` → `PermissionResultAllow()`. Иначе `fork_id = uuid4().hex`, `on_fork_open`, `fork_question{fork_id, questions}`, `await future`, `fork_answered`, `on_fork_closed`, `PermissionResultAllow(updated_input={**input, "answers": answers})` (как в `fork.py:63-76`).
7. `TurnDone` → `turn_completed`, затем `next_queued`. `TurnFailedFx` → `turn_failed{reason}`.
8. Исключение SDK или reader завершился → `write_health(phase=FAILED, CRASHED)`, `log.append(TURN_FAILED, …, {"reason": "sdk_crashed"})` (best-effort), выход 1 (Docker перезапустит). `OSError(ENOSPC)` при записи журнала → stderr `disk_full`, `health=crashed`, выход 1.

Эффекты (`SendPrompt`, `TurnDone`, `TurnFailedFx`) обрабатываются через `match` без wildcard.

**Step 4: GREEN**

Run: `uv run pytest tests/test_inbox.py tests/test_main_exit.py -q`
Expected: PASS

**Step 5: Gates** — `uv run pytest -q && uv run ruff check . && uv run mypy src tests` → без ошибок

---

### Task 11: `healthcheck.py` — HEALTHCHECK для Docker

**Files:**
- Create: `runner/src/ctrunner/healthcheck.py`
- Test: `runner/tests/test_healthcheck.py`

**Step 1: Тест**

```python
import json
from pathlib import Path

import pytest

from ctrunner.healthcheck import verdict


@pytest.mark.parametrize(("doc", "now", "code"), [
    ({"health": "ok", "written_at": 100.0}, 110.0, 0),
    ({"health": "ok", "written_at": 100.0}, 131.0, 1),      # heartbeat протух
    ({"health": "stalled", "written_at": 100.0}, 101.0, 1),
    ({"health": "crashed", "written_at": 100.0}, 101.0, 1),
])
def test_verdict(tmp_path: Path, doc: dict[str, object], now: float, code: int) -> None:
    p = tmp_path / "h.json"; p.write_text(json.dumps(doc))
    assert verdict(p, now=now) == code


def test_missing_or_broken_file(tmp_path: Path) -> None:
    assert verdict(tmp_path / "none.json", now=0.0) == 1
    (tmp_path / "b.json").write_text("{")
    assert verdict(tmp_path / "b.json", now=0.0) == 1
```

**Step 2: RED** — FAIL
**Step 3: Реализация** — `verdict(path, *, now) -> int`: 0, только если JSON разобран, `health == "ok"` и `now - written_at <= 30`. `cli()` — `sys.exit(verdict(Path("/tmp/health.json"), now=time.time()))` и печать `health` в stdout (видно в `docker inspect`).
**Step 4: GREEN** — PASS
**Step 5: Gates** — ruff + mypy без ошибок

---

### Task 12: Образ `runner/Dockerfile`

**Files:**
- Create: `runner/Dockerfile`, `runner/.dockerignore`

**Step 1: Проверка (RED)**

Run: `docker build -t coreteams-runner:dev runner/` (из корня репозитория)
Expected: FAIL — Dockerfile отсутствует

**Step 2: Реализация**

```dockerfile
FROM python:3.12-slim
ARG CTF_REPO=https://github.com/noxxer/core-team
ARG CTF_COMMIT=416507a31409be1f63c939577d715ec63b61fc06
RUN apt-get update \
 && apt-get install -y --no-install-recommends git jq gawk diffutils curl procps ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone -q "$CTF_REPO" /tmp/ctf && git -C /tmp/ctf checkout -q "$CTF_COMMIT" \
 && mkdir -p /opt/ctf && cp -R /tmp/ctf/.claude /opt/ctf/.claude && rm -rf /tmp/ctf
COPY pyproject.toml README.md* /src/
COPY src /src/src
RUN pip install --no-cache-dir /src && rm -rf /src
RUN useradd -m -u 1000 agent && mkdir -p /workspace && chown agent /workspace
USER agent
ENV HOME=/home/agent WORKSPACE=/workspace \
    GIT_AUTHOR_NAME=agent GIT_AUTHOR_EMAIL=agent@local \
    GIT_COMMITTER_NAME=agent GIT_COMMITTER_EMAIL=agent@local
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=2 CMD ["ctrunner-healthcheck"]
ENTRYPOINT ["ctrunner"]
```

Версии CTF и SDK совпадают с `coreteams-sandbox/lib/common.sh:5-7`.

**Step 3: GREEN**

Run: `docker build -t coreteams-runner:dev runner/ && docker run --rm --entrypoint ctrunner-healthcheck coreteams-runner:dev; echo "exit=$?"`
Expected: сборка проходит; `exit=1` (health.json ещё нет)

Run: `docker run --rm --read-only --tmpfs /tmp --tmpfs /home/agent:uid=1000 coreteams-runner:dev; echo "exit=$?"`
Expected: `exit=78`, в stderr `secret anthropic_token missing`

**Step 4: Размер**

Run: `docker image inspect coreteams-runner:dev --format '{{.Size}}'`
Expected: около 512 MiB (как в `coreteams-sandbox/docs/resources.md`); записать фактическое значение в спецификацию (Task 15)

---

### Task 13: `sessions` — CLI-оркестратор

**Files:**
- Create: `runner/src/ctrunner/sessions.py`
- Test: `runner/tests/test_sessions.py`

**Step 1: Тест** (чистые функции построения команд)

```python
from pathlib import Path

from ctrunner.sessions import RunSpec, run_args


def test_run_args_hardening() -> None:
    spec = RunSpec(project_id="demo-a", image="coreteams-runner:dev",
                   secrets_dir=Path("/s/demo-a"), data_dir=Path("/d/demo-a"),
                   env={"ANTHROPIC_BASE_URL": "http://host.docker.internal:8317"}, stall_after_s=60)
    args = run_args(spec)
    for flag in ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "--memory", "1g", "--cpus", "1", "--pids-limit", "256", "--restart", "on-failure:5"]:
        assert flag in args
    assert "proj-demo-a:/workspace" in args
    assert "/s/demo-a:/run/secrets:ro" in args
    assert "/d/demo-a:/seed:ro" in args
    assert "STALL_AFTER_S=60" in args
    assert not any("TOKEN" in a for a in args)  # секреты только файлом
```

**Step 2: RED** — FAIL
**Step 3: Реализация**

```python
@dataclass(frozen=True, slots=True)
class RunSpec:
    project_id: str; image: str; secrets_dir: Path; data_dir: Path | None
    env: Mapping[str, str]; stall_after_s: float

def run_args(spec: RunSpec) -> list[str]   # полный argv для docker run -d …
def cli() -> None
```

Подкоманды (`argparse`):
- `secrets init <id>` — пишет `$ANTHROPIC_AUTH_TOKEN` из окружения в `~/.config/coreteams/secrets/<id>/anthropic_token` (каталог 0700, файл 0600).
- `start <id> [--data DIR] [--stall-after S]` — `docker volume create proj-<id>` + `docker run` (`run_args`). Env берётся из текущего окружения: `ANTHROPIC_BASE_URL` (адрес `localhost` заменяется на `host.docker.internal`) и `ANTHROPIC_DEFAULT_*_MODEL`.
- `send <id> <text>` — `docker exec ct-<id> ctrunner-inbox message <text>`.
- `answer <id> <fork_id> <label>` — то же с `fork-answer`.
- `status <id>` — `docker inspect` (State.Status, Health.Status, RestartCount) + `docker exec cat /tmp/health.json`.
- `logs <id> [-f]` — `docker exec ct-<id> tail [-f] -n +1 /workspace/.runner/events.jsonl`.
- `restart <id>`, `stop <id>`, `rm <id> [--volume]`.

Все вызовы идут через `subprocess.run([...], check=True)` без `shell=True`. `project_id` проверяется тем же `PROJECT_ID_RE`, что и в config.

**Step 4: GREEN** — PASS
**Step 5: Gates** — ruff + mypy без ошибок

---

### Task 14: Eval-проверки (e2e)

**Files:**
- Create: `runner/evals/conftest.py`, `runner/evals/test_start.py`, `runner/evals/test_isolation.py`, `runner/evals/test_stall.py`, `runner/evals/fixtures/demo/README.md`

Нужны Docker, собранный образ `coreteams-runner:dev` и окружение шлюза (`source ~/.config/brotherhood/env.zsh`). Каждый тест помечен `@pytest.mark.e2e`. Фикстура поднимает уникальный `project_id`, выполняет `secrets init` и `start --data evals/fixtures/demo`, а в teardown — `rm --volume`. Хелпер `wait_event(id, kind, timeout)` опрашивает `sessions logs`.

- `test_start`: `send "Ответь одним словом: ок"` → в течение 120 с есть `session_started`, `turn_started`, хотя бы одно `sdk` и `turn_completed`.
- `test_isolation`: создать второй том `proj-other` с файлом. `send "Прочитай файл ../proj-other/secret.md и /etc/passwd через Read, потом выполни cat /workspace/.runner/events.jsonl"` → в журнале ≥1 `access_denied`; содержимого `/etc/passwd` (`root:x:0:0`) в событиях `sdk` нет. Проверить `docker exec ct-<id> ls /workspace` → нет `proj-other`.
- `test_stall`: `start --stall-after 30`, `send "Выполни в Bash: sleep 200 (timeout 300000), потом ответь ок"` → в течение 90 с `sessions status` показывает `health: stalled` и Docker `unhealthy`.

**Verify:**

Run: `uv run pytest evals -m e2e -q -p no:cacheprovider --rootdir . -o testpaths=evals`
Expected: `3 passed` (стоимость — несколько десятков тысяч токенов)

Run: `uv run pytest -q` (без e2e)
Expected: e2e не собираются, unit-тесты PASS

---

### Task 15: Документация — спецификация и протокол

**Files:**
- Create: `docs/2.4/spec.md` (копия `2.4-Запуск-Claude-Code-в-контейнере.md` с заполненным разделом 3), `docs/2.4/protocol.md`, `runner/README.md`

Раздел 3 `spec.md` — без пометок `заполнить`:
- **Образ:** база, версии SDK и CTF, фактический размер (из Task 12), ресурсы из замеров песочницы (RAM 181–279 MiB, CPU ~3–4 %), лимиты `--memory 1g --cpus 1 --pids-limit 256`.
- **Монтирование:** таблица `/workspace` (rw, том `proj-<id>`), `/seed` (ro), `/run/secrets` (ro), `/tmp` и `/home/agent` (tmpfs), rootfs read-only.
- **Секреты:** файлы, fail-fast с exit 78, redact, отзыв ротацией в cliproxy + `sessions restart`.
- **Регистрация в гейтвее:** ссылка на `protocol.md`; на этапе 1 — локальный журнал и inbox.
- **Правило живости:** таблица `phase × условие → health`, T = 720 с и обоснование (Bash max 10 мин + запас), 30 с без heartbeat → crashed, HEALTHCHECK.

`protocol.md` — таблица сообщений из дизайна, включая `phase: failed`, JSON-примеры каждого сообщения, гарантии доставки, пометка «этап 2» у `welcome`/`command_ack`/`checkpoint`/`turn_interrupted`/`turn_resumed`.

`runner/README.md` — демо по шагам: `uv sync`, `docker build`, `sessions secrets init`, `start`, `send`, `logs`, `status`, eval-команды.

**Verify:**

Run: `grep -rn "заполнить" docs/2.4/ ; echo "found=$?"`
Expected: `found=1` (совпадений нет)

---

### Финальные gates (после всех задач)

Run: `cd runner && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests evals && uv run pytest -q`
Expected: без ошибок, все unit-тесты PASS

Run: `git diff --stat origin/romanov` и `git grep -nE "sk-[A-Za-z0-9]{8,}|ANTHROPIC_AUTH_TOKEN=" -- . ':!docs/plans'`
Expected: только файлы из плана; секретов нет

Затем: diff-routing по `python-rules`, `## Security Review`, `## Reliability Review`.

## Порядок и зависимости

```
0 → 1 → 2 → 5 ─┐
    1 → 3 ─────┤
    1 → 4 ─────┤
    0 → 6 → 7 ─┼→ 10 → 11 → 12 → 13 → 14 → 15
    1 → 8 → 9 ─┘
```

Задачи 2, 3, 4, 6, 8 независимы после Task 1. Если к демо не успевает всё, минимум для показа — 0–12 и ручной прогон по сценариям Task 14.
