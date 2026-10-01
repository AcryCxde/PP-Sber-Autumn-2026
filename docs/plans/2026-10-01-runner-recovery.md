# Восстановление runner после рестарта — план реализации

> **Для исполнителя:** выполняйте план задача за задачей через `executing-plans`. Дизайн: [2026-10-01-runner-recovery-design.md](2026-10-01-runner-recovery-design.md).

**Цель:** после рестарта контейнера runner определяет исход прерванного хода (завершён / продолжен / провален) по `state.json` и git, не теряет подтверждённую работу и команды.

**Архитектура:** `state.json` хранит открытый ход, очередь и `session_id`; конец хода фиксируется git-коммитом с trailer `Turn-Id`; чистая функция `recovery.plan` по состоянию и `HEAD` выбирает `Clean | Finalize | Continue | GiveUp`; `Session` (императивная оболочка) применяет решение. Порядок записи: при закрытии хода commit → события → state, при открытии — state → `query`; ack команды в inbox — после записи state.

**Стек:** Python 3.12, `claude-agent-sdk==0.2.158`, pytest (`-W error`, `asyncio_mode=auto`), ruff, mypy strict, uv. Рабочий каталог команд: `runner/`.

**Ограничения:**
- Ветка `PPS-116-runner-recovery` от `master` (MR #3 уже влит). MR в `master`, в описании `Closes PPS-116, PPS-133`.
- Правила `python-rules`: ветка исключений (Result-библиотек в зависимостях нет); закрытые объединения — `@final` + `assert_never`; чистое ядро, I/O в оболочке; новые зависимости не добавляются.
- Комментарии и тексты событий — по-русски, как в соседнем коде. Коммиты — `feat(runner): … (Task N)` в стиле истории.
- Задачи 5–7 ломают сборку друг друга (inbox → turn → session). Между ними запускаются только перечисленные тесты, коммит — один, в конце задачи 7.

**Не делаем:** WS-клиент и повтор событий (MR #4), откат дерева, автоубийство зависших сессий, сохранение развилок между рестартами (развилку прерывает рестарт → `turn_interrupted` → агент спрашивает заново).

---

### Task 0: Спайк — поведение `resume` в SDK

Цель: до кода проверить три факта, от которых зависят задачи 4 и 8. Результат записывается в дизайн (`## Findings`). Нужны `source ~/.config/brotherhood/env.zsh` и `ANTHROPIC_AUTH_TOKEN`; без них отметьте пункты `unverified:` и переходите дальше (план остаётся рабочим, фолбэк `mode=fresh` покрывает отказ).

**Files:**
- Create (вне репозитория, не коммитить): `$TMPDIR/spike_resume.py`
- Modify: `docs/plans/2026-10-01-runner-recovery-design.md` (раздел `## Findings` в конец)

**Step 1: Скрипт**

```python
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient


async def ask(options: ClaudeAgentOptions, prompt: str) -> str | None:
    session_id: str | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            session_id = getattr(message, "session_id", None) or session_id
            print(type(message).__name__, str(message)[:160])
    return session_id


async def main() -> None:
    config = Path(tempfile.mkdtemp())
    work = Path(tempfile.mkdtemp())
    env = {"CLAUDE_CONFIG_DIR": str(config)}

    def opts(resume: str | None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(cwd=work, env=env, resume=resume)

    sid = await ask(opts(None), "Запомни слово ананас. Ответь: ок")
    print("A session_id:", sid)
    print("A transcripts:", [str(p.relative_to(config)) for p in config.rglob("*.jsonl")])
    await ask(opts(sid), "Какое слово я просил запомнить?")  # B: ожидаем «ананас»
    try:  # C: resume несуществующей сессии
        await ask(opts("00000000-0000-0000-0000-000000000000"), "привет")
    except Exception as error:  # исследуем тип ошибки
        print("C error:", type(error).__name__, error)
    print("config dir:", config, file=sys.stderr)


asyncio.run(main())
```

**Step 2: Запуск**

Run: `cd runner && source ~/.config/brotherhood/env.zsh && uv run python $TMPDIR/spike_resume.py`
Expected: напечатаны `session_id`, путь транскрипта, ответ B, результат C.

**Step 3: Прерванный вызов инструмента (D), вручную**

1. В скрипте замените `ask(opts(None), …)` на промпт «Выполни Bash: `sleep 120`» и запустите.
2. Во втором терминале убейте бинарник CLI из venv этого проекта: `pkill -9 -f "$(cd runner && uv run python -c 'import claude_agent_sdk,pathlib;print(pathlib.Path(claude_agent_sdk.__file__).parent/"_bundled"/"claude")')"` (путь проверьте `ls`, в 0.2.158 бинарник лежит внутри пакета; чужие процессы Claude Code не трогайте).
3. Возьмите `session_id` из вывода и выполните `ask(opts(sid), "Продолжи")`.

**Step 4: Записать находки**

В `## Findings` дизайна: (1) в каком сообщении первым приходит `session_id`; (2) путь транскрипта (ожидаем `<CLAUDE_CONFIG_DIR>/projects/<slug>/<session_id>.jsonl`); (3) C: ошибка при `connect`, при первом `query` или молчаливая новая сессия; (4) D: принимает ли API `resume` после оборванного tool call.

Правила последствий:

| Находка | Последствие |
|---|---|
| (2) путь отличается от `projects/*/<id>.jsonl` | поправить glob в `resumable_session` (Task 8) |
| (3) ошибка при `connect` | в Task 8 выполнить условный шаг «фолбэк на fresh» |
| (4) resume после оборванного вызова даёт ошибку API | в Task 8 `resumable` всегда `None` (режим `fresh` основной), записать в README |
| env недоступен | пометить `unverified:` и продолжить |

**Step 5: Commit**

Run: `git add docs/plans && git commit -m "docs(2.4): design and plan for runner recovery"`
Expected: коммит с дизайном, планом и Findings.

---

### Task 1: Виды событий и чтение журнала по ходу

**Files:**
- Modify: `runner/src/ctrunner/protocol.py:36-44` (`EventKind`)
- Modify: `runner/src/ctrunner/eventlog.py` (новая функция в конец файла)
- Test: `runner/tests/test_eventlog.py`

**Step 1: Write the failing test** (добавить в конец `tests/test_eventlog.py`; импорты `json`, `Path`, `EventKind` проверьте в шапке файла и добавьте недостающие)

```python
from ctrunner.eventlog import kinds_of_turn


def test_kinds_of_turn_collects_only_that_turn(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    rows = [
        {"seq": 1, "turn_id": "t1", "kind": "turn_started"},
        {"seq": 2, "turn_id": "t2", "kind": "checkpoint"},
        {"seq": 3, "turn_id": "t1", "kind": "checkpoint"},
        {"seq": 4, "turn_id": None, "kind": "session_started"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert kinds_of_turn(path, "t1") == {EventKind.TURN_STARTED, EventKind.CHECKPOINT}


def test_kinds_of_turn_without_log_is_empty(tmp_path: Path) -> None:
    assert kinds_of_turn(tmp_path / "missing.jsonl", "t1") == frozenset()


def test_recovery_event_kinds_are_serialised() -> None:
    assert {EventKind.CHECKPOINT, EventKind.TURN_INTERRUPTED, EventKind.TURN_RESUMED} == {
        EventKind("checkpoint"),
        EventKind("turn_interrupted"),
        EventKind("turn_resumed"),
    }
```

**Step 2: Verify RED**

Run: `cd runner && uv run pytest tests/test_eventlog.py -q`
Expected: FAIL: `ImportError: cannot import name 'kinds_of_turn'`.

**Step 3: Implement**

`protocol.py`: в `EventKind` после `ACCESS_DENIED` добавить

```python
    CHECKPOINT = "checkpoint"
    TURN_INTERRUPTED = "turn_interrupted"
    TURN_RESUMED = "turn_resumed"
```

`eventlog.py` (в конец; вызывается после `EventLog.open`, который уже отрезал недописанную строку):

```python
def kinds_of_turn(path: Path, turn_id: str) -> frozenset[EventKind]:
    """Какие события хода уже в журнале: восстановление дописывает только недостающие."""
    kinds: set[EventKind] = set()
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if row.get("turn_id") == turn_id:
                    kinds.add(EventKind(row["kind"]))
    except FileNotFoundError:
        return frozenset()
    return frozenset(kinds)
```

**Step 4: Verify GREEN**

Run: `cd runner && uv run pytest tests/test_eventlog.py -q`
Expected: PASS.

**Step 5: Gates**

Run: `cd runner && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/ctrunner/eventlog.py tests/test_eventlog.py`
Expected: PASS без новых предупреждений.

**Step 6: Commit** — `git add -A && git commit -m "feat(runner): recovery event kinds and per-turn log lookup (Task 1)"`

---

### Task 2: `state.json` — типы, разбор, атомарная запись

**Files:**
- Create: `runner/src/ctrunner/state.py`
- Test: `runner/tests/test_state.py`

**Step 1: Write the failing test** (`tests/test_state.py`)

```python
import json
from pathlib import Path

import pytest

from ctrunner.protocol import MessageCmd
from ctrunner.state import (
    EMPTY,
    OpenTurn,
    PersistedState,
    StateCorrupt,
    is_session_id,
    load_state,
    parse_state,
    save_state,
)

FULL = PersistedState(
    session_id="749df2fe-1b50-4c46-92f3-e2cdf0ce322d",
    open_turn=OpenTurn("t1", MessageCmd("c1", "go")),
    last_sha="a" * 40,
    autoresume_count=2,
    queue=(MessageCmd("c2", "next"),),
)


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, FULL)
    assert load_state(path) == FULL
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # tmp-файлов нет


def test_missing_file_is_empty_state(tmp_path: Path) -> None:
    assert load_state(tmp_path / "state.json") == EMPTY


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "[]",
        json.dumps({"v": 2}),
        json.dumps({"v": 1, "session_id": 5}),
        json.dumps({"v": 1, "session_id": "bad id!", "open_turn": None, "last_sha": None,
                    "autoresume_count": 0, "queue": []}),
        json.dumps({"v": 1, "session_id": None, "open_turn": None, "last_sha": None,
                    "autoresume_count": -1, "queue": []}),
        json.dumps({"v": 1, "session_id": None, "open_turn": None, "last_sha": None,
                    "autoresume_count": 1, "queue": []}),  # счётчик без открытого хода
        json.dumps({"v": 1, "session_id": None, "open_turn": None, "last_sha": None,
                    "autoresume_count": 0, "queue": [{"id": "c"}]}),
    ],
)
def test_corrupt_state_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(StateCorrupt):
        load_state(path)


def test_parse_state_accepts_what_save_writes() -> None:
    from ctrunner.state import to_json

    assert parse_state(to_json(FULL)) == FULL


def test_session_id_shape() -> None:
    assert is_session_id("749df2fe-1b50-4c46-92f3-e2cdf0ce322d")
    assert not is_session_id("../x")
    assert not is_session_id("")
```

**Step 2: Verify RED**

Run: `cd runner && uv run pytest tests/test_state.py -q`
Expected: FAIL: `ModuleNotFoundError: No module named 'ctrunner.state'`.

**Step 3: Implement** (`src/ctrunner/state.py`)

```python
"""Состояние восстановления `state.json`: всё, что runner должен пережить после рестарта."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, final

from ctrunner.fsio import write_json_atomic
from ctrunner.protocol import JsonValue, MessageCmd

STATE_VERSION: Final = 1
# Идентификатор подставляется в glob транскрипта: метасимволов в нём быть не может.
SESSION_ID_RE: Final = re.compile(r"[0-9A-Za-z_-]{1,64}")


class StateCorrupt(Exception):
    """`state.json` нельзя разобрать: решение о ходе без него было бы догадкой."""


def is_session_id(value: str) -> bool:
    return SESSION_ID_RE.fullmatch(value) is not None


@final
@dataclass(frozen=True, slots=True)
class OpenTurn:
    turn_id: str
    cmd: MessageCmd


@final
@dataclass(frozen=True, slots=True)
class PersistedState:
    session_id: str | None
    open_turn: OpenTurn | None
    last_sha: str | None
    autoresume_count: int  # подряд идущие автопродолжения открытого хода
    queue: tuple[MessageCmd, ...]

    def __post_init__(self) -> None:
        if self.session_id is not None and not is_session_id(self.session_id):
            raise ValueError("session_id has unexpected shape")
        if self.autoresume_count < 0:
            raise ValueError("autoresume_count must be non-negative")
        if self.open_turn is None and self.autoresume_count:
            raise ValueError("autoresume_count requires an open turn")


EMPTY: Final = PersistedState(None, None, None, 0, ())


def _cmd_json(cmd: MessageCmd) -> dict[str, JsonValue]:
    return {"id": cmd.id, "text": cmd.text}


def to_json(state: PersistedState) -> dict[str, JsonValue]:
    open_turn: JsonValue = (
        None
        if state.open_turn is None
        else {"turn_id": state.open_turn.turn_id, "cmd": _cmd_json(state.open_turn.cmd)}
    )
    return {
        "v": STATE_VERSION,
        "session_id": state.session_id,
        "open_turn": open_turn,
        "last_sha": state.last_sha,
        "autoresume_count": state.autoresume_count,
        "queue": [_cmd_json(c) for c in state.queue],
    }


def save_state(path: Path, state: PersistedState) -> None:
    write_json_atomic(path, to_json(state))


def load_state(path: Path) -> PersistedState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return EMPTY
    except (OSError, ValueError) as error:  # ValueError покрывает JSONDecodeError и UnicodeDecodeError
        raise StateCorrupt(f"{path.name}: {error}") from error
    return parse_state(raw)


def parse_state(raw: object) -> PersistedState:
    """Граница: нетипизированный JSON → доменный тип. Любое несоответствие — `StateCorrupt`."""
    try:
        return _parse(raw)
    except ValueError as error:
        raise StateCorrupt(str(error)) from error


def _parse(raw: object) -> PersistedState:
    doc = _object(raw)
    if doc.get("v") != STATE_VERSION:
        raise ValueError(f"unsupported state version: {doc.get('v')!r}")
    open_raw = doc.get("open_turn")
    return PersistedState(
        session_id=_opt_str(doc, "session_id"),
        open_turn=None if open_raw is None else _open_turn(open_raw),
        last_sha=_opt_str(doc, "last_sha"),
        autoresume_count=_int(doc, "autoresume_count"),
        queue=tuple(_cmd(item) for item in _list(doc, "queue")),
    )


def _object(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("expected an object")
    return {str(k): v for k, v in raw.items()}


def _list(doc: dict[str, object], key: str) -> list[object]:
    value = doc.get(key)
    if not isinstance(value, list):
        raise ValueError(f"field {key!r} must be an array")
    return value


def _str(doc: dict[str, object], key: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"field {key!r} must be a non-empty string")
    return value


def _opt_str(doc: dict[str, object], key: str) -> str | None:
    return None if doc.get(key) is None else _str(doc, key)


def _int(doc: dict[str, object], key: str) -> int:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"field {key!r} must be an integer")
    return value


def _cmd(raw: object) -> MessageCmd:
    doc = _object(raw)
    return MessageCmd(_str(doc, "id"), _str(doc, "text"))


def _open_turn(raw: object) -> OpenTurn:
    doc = _object(raw)
    return OpenTurn(_str(doc, "turn_id"), _cmd(doc.get("cmd")))
```

**Step 4: Verify GREEN**

Run: `cd runner && uv run pytest tests/test_state.py -q`
Expected: PASS (13 passed).

**Step 5: Gates**

Run: `cd runner && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/ctrunner/state.py tests/test_state.py`
Expected: PASS. Если ruff ругается на формат длинных `json.dumps` в параметрах — `uv run ruff format tests/test_state.py`.

**Step 6: Commit** — `git add -A && git commit -m "feat(runner): persisted recovery state (Task 2)"`

---

### Task 3: Checkpoint — commit с trailer, чтение `HEAD`, stale lock

**Files:**
- Create: `runner/src/ctrunner/checkpoint.py`
- Modify: `runner/src/ctrunner/main.py:30-31` (удалить `GIT_TIMEOUT_S`), `:91-122` (`_git` → `run_git` из `checkpoint`)
- Test: `runner/tests/test_checkpoint.py`

**Step 1: Write the failing test** (`tests/test_checkpoint.py`)

```python
import subprocess
from pathlib import Path

import pytest

from ctrunner import checkpoint
from ctrunner.checkpoint import (
    CheckpointError,
    CheckpointFailure,
    Head,
    clear_stale_lock,
    commit_turn,
    read_head,
    run_git,
)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@local")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    run_git(tmp_path, "init", "-q")
    run_git(tmp_path, "commit", "-q", "--allow-empty", "-m", "init")
    return tmp_path


def test_initial_head_has_no_turn(repo: Path) -> None:
    head = read_head(repo)
    assert head.turn_id is None
    assert len(head.sha) == 40


def test_commit_turn_records_files_and_trailer(repo: Path) -> None:
    (repo / "a.txt").write_text("one")
    sha = commit_turn(repo, "t1")
    assert read_head(repo) == Head(sha, "t1")
    tracked = run_git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.decode()
    assert "a.txt" in tracked


def test_commit_turn_without_changes_still_marks_turn(repo: Path) -> None:
    before = read_head(repo).sha
    sha = commit_turn(repo, "t2")
    assert sha != before
    assert read_head(repo).turn_id == "t2"


def test_repo_hooks_do_not_block_checkpoint(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    assert commit_turn(repo, "t3")


def test_clear_stale_lock(repo: Path) -> None:
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    assert clear_stale_lock(repo) is True
    assert not lock.exists()
    assert clear_stale_lock(repo) is False


def _fail_with(monkeypatch: pytest.MonkeyPatch, stderr: bytes) -> None:
    def boom(project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.CalledProcessError(128, ["git", *args], stderr=stderr)

    monkeypatch.setattr(checkpoint, "run_git", boom)


def test_disk_full_is_classified(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_with(monkeypatch, b"error: No space left on device")
    with pytest.raises(CheckpointError) as caught:
        commit_turn(repo, "t4")
    assert caught.value.failure is CheckpointFailure.DISK_FULL


def test_other_git_failure_is_classified(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_with(monkeypatch, b"fatal: something else")
    with pytest.raises(CheckpointError) as caught:
        commit_turn(repo, "t5")
    assert caught.value.failure is CheckpointFailure.FAILED
```

**Step 2: Verify RED**

Run: `cd runner && uv run pytest tests/test_checkpoint.py -q`
Expected: FAIL: `ModuleNotFoundError: No module named 'ctrunner.checkpoint'`.

**Step 3: Implement** (`src/ctrunner/checkpoint.py`)

```python
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
        failure = CheckpointFailure.DISK_FULL if error.errno == errno.ENOSPC else CheckpointFailure.FAILED
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
```

`main.py`: удалить константу `GIT_TIMEOUT_S` и функцию `_git`; добавить `from ctrunner.checkpoint import run_git`; в `seed_project` заменить вызовы `_git(` на `run_git(`. Убрать ставший неиспользуемым `import subprocess`, если ruff на него укажет (`uv run ruff check --fix src/ctrunner/main.py`).

**Step 4: Verify GREEN**

Run: `cd runner && uv run pytest tests/test_checkpoint.py tests/test_seed.py -q`
Expected: PASS (`test_seed` подтверждает, что рефакторинг `main.py` ничего не сломал).

**Step 5: Gates**

Run: `cd runner && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/ctrunner/checkpoint.py src/ctrunner/main.py tests/test_checkpoint.py`
Expected: PASS. Длинную строку `failure = …` ruff format перенесёт сам.

**Step 6: Commit** — `git add -A && git commit -m "feat(runner): git checkpoint with Turn-Id trailer (Task 3)"`

---

### Task 4: Планировщик восстановления (чистая функция)

**Files:**
- Create: `runner/src/ctrunner/recovery.py`
- Test: `runner/tests/test_recovery.py`

**Step 1: Write the failing test**

```python
import pytest

from ctrunner.checkpoint import Head
from ctrunner.protocol import MessageCmd
from ctrunner.recovery import (
    MAX_AUTORESUME,
    Clean,
    Continue,
    Finalize,
    GiveUp,
    ResumeMode,
    continuation_prompt,
    plan,
)
from ctrunner.state import EMPTY, OpenTurn, PersistedState

CMD = MessageCmd("c1", "собери отчёт")
SHA = "a" * 40


def interrupted(count: int = 0) -> PersistedState:
    return PersistedState("sess-1", OpenTurn("t1", CMD), None, count, ())


def test_no_open_turn_is_clean() -> None:
    assert plan(EMPTY, Head(SHA, None), resumable=True) == Clean()


def test_commit_of_open_turn_means_finalize_even_at_limit() -> None:
    state = interrupted(MAX_AUTORESUME)
    assert plan(state, Head(SHA, "t1"), resumable=True) == Finalize("t1", SHA)


def test_other_turn_trailer_does_not_finalize() -> None:
    assert isinstance(plan(interrupted(), Head(SHA, "t0"), resumable=True), Continue)


@pytest.mark.parametrize(
    ("resumable", "mode"), [(True, ResumeMode.RESUME), (False, ResumeMode.FRESH)]
)
def test_continue_mode_follows_transcript(resumable: bool, mode: ResumeMode) -> None:
    result = plan(interrupted(), Head(SHA, None), resumable=resumable)
    assert result == Continue("t1", CMD, 1, mode)


def test_attempt_counts_up_to_limit_then_gives_up() -> None:
    last = plan(interrupted(MAX_AUTORESUME - 1), Head(SHA, None), resumable=True)
    assert isinstance(last, Continue)
    assert last.attempt == MAX_AUTORESUME
    assert plan(interrupted(MAX_AUTORESUME), Head(SHA, None), resumable=True) == GiveUp("t1")


@pytest.mark.parametrize("mode", list(ResumeMode))
def test_prompt_keeps_task_and_asks_to_check_tree(mode: ResumeMode) -> None:
    text = continuation_prompt(Continue("t1", CMD, 1, mode))
    assert CMD.text in text
    assert "git status" in text
    assert "git diff" in text
```

**Step 2: Verify RED**

Run: `cd runner && uv run pytest tests/test_recovery.py -q`
Expected: FAIL: `ModuleNotFoundError: No module named 'ctrunner.recovery'`.

**Step 3: Implement** (`src/ctrunner/recovery.py`)

```python
"""Планировщик восстановления: что делать с ходом, застигнутым рестартом. Без ввода-вывода."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, assert_never, final

from ctrunner.checkpoint import Head
from ctrunner.protocol import MessageCmd
from ctrunner.state import PersistedState

MAX_AUTORESUME: Final = 3


class ResumeMode(StrEnum):
    RESUME = "resume"  # SDK-сессия продолжается по session_id
    FRESH = "fresh"  # транскрипта нет: новая сессия, задача повторяется


@final
@dataclass(frozen=True, slots=True)
class Clean:
    """Прерванного хода нет."""


@final
@dataclass(frozen=True, slots=True)
class Finalize:
    """Commit хода успел, не успели события и state: ход завершён, повторять нельзя."""

    turn_id: str
    sha: str


@final
@dataclass(frozen=True, slots=True)
class Continue:
    turn_id: str
    cmd: MessageCmd
    attempt: int
    mode: ResumeMode


@final
@dataclass(frozen=True, slots=True)
class GiveUp:
    """Лимит автопродолжений исчерпан: ход провален, дальше решает человек."""

    turn_id: str


type Recovery = Clean | Finalize | Continue | GiveUp

CLEAN: Final = Clean()


def plan(state: PersistedState, head: Head, *, resumable: bool) -> Recovery:
    turn = state.open_turn
    if turn is None:
        return CLEAN
    if head.turn_id == turn.turn_id:
        return Finalize(turn.turn_id, head.sha)
    if state.autoresume_count >= MAX_AUTORESUME:
        return GiveUp(turn.turn_id)
    mode = ResumeMode.RESUME if resumable else ResumeMode.FRESH
    return Continue(turn.turn_id, turn.cmd, state.autoresume_count + 1, mode)


def continuation_prompt(step: Continue) -> str:
    """Задача повторяется целиком в обоих режимах: модель могла не успеть её увидеть."""
    match step.mode:
        case ResumeMode.RESUME:
            preface = "Работа над задачей прервана перезапуском контейнера."
        case ResumeMode.FRESH:
            preface = (
                "Работа над задачей прервана перезапуском контейнера, "
                "контекст прежней сессии потерян."
            )
        case _:
            assert_never(step.mode)
    return (
        f"{preface} Сначала проверь `git status` и `git diff`: часть работы уже сделана, "
        f"не повторяй её.\n\nЗадача:\n{step.cmd.text}"
    )
```

**Step 4: Verify GREEN**

Run: `cd runner && uv run pytest tests/test_recovery.py -q`
Expected: PASS.

**Step 5: Gates**

Run: `cd runner && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/ctrunner/recovery.py tests/test_recovery.py`
Expected: PASS.

**Step 6: Commit** — `git add -A && git commit -m "feat(runner): pure recovery planner (Task 4)"`

---

### Task 5: Двухфазный inbox (`peek` + `ack`)

Зависимость: после этой задачи `session.py` не собирается до Task 7. Запускайте только `tests/test_inbox.py`; коммита нет.

**Files:**
- Modify: `runner/src/ctrunner/inbox.py:1-5` (docstring), `:34-65` (`Inbox`)
- Test: `runner/tests/test_inbox.py`

**Step 1: Write the failing test**

В `tests/test_inbox.py` добавьте помощник и замените все вызовы `X.take()` на `drain(X)` (строки 13, 14, 20, 26, 33, 35, 40, 42, 64):

```python
from ctrunner.protocol import Command


def drain(box: Inbox) -> list[Command]:
    deliveries = box.peek()
    for delivery in deliveries:
        box.ack(delivery)
    return [d.command for d in deliveries]
```

Новые тесты:

```python
def test_peek_without_ack_redelivers(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "go"})
    box = Inbox(tmp_path)
    first = box.peek()
    assert [d.command for d in first] == [MessageCmd("c1", "go")]
    assert [d.command for d in box.peek()] == [MessageCmd("c1", "go")]  # файл ещё на месте
    box.ack(first[0])
    assert box.peek() == []
    assert len(list((tmp_path / "processed").iterdir())) == 1


def test_unacked_command_survives_restart(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "go"})
    Inbox(tmp_path).peek()  # процесс упал до ack
    assert [d.command for d in Inbox(tmp_path).peek()] == [MessageCmd("c1", "go")]


def test_same_id_twice_in_one_batch_is_delivered_once(tmp_path: Path) -> None:
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    put(tmp_path, {"id": "c1", "kind": "message", "text": "a"})
    assert len(Inbox(tmp_path).peek()) == 1
```

**Step 2: Verify RED**

Run: `cd runner && uv run pytest tests/test_inbox.py -q`
Expected: FAIL: `AttributeError: 'Inbox' object has no attribute 'peek'`.

**Step 3: Implement**

`inbox.py`: заменить `take` на

```python
@final
@dataclass(frozen=True, slots=True)
class Delivery:
    command: Command
    path: Path
```

(добавить `from dataclasses import dataclass`), а в `Inbox`:

```python
    def peek(self) -> list[Delivery]:
        """Команды без переноса: файл остаётся в inbox, пока `ack` не подтвердит запись состояния."""
        deliveries: list[Delivery] = []
        batch: set[str] = set()
        for path in sorted(self._dir.glob("[!.]*.json")):
            try:
                command = parse_command(json.loads(path.read_text(encoding="utf-8")))
            except (ProtocolError, ValueError) as error:
                os.replace(path, self._rejected / path.name)
                diag("command_rejected", file=path.name, reason=str(error))
                continue
            if command.id in self._seen or command.id in batch:
                os.replace(path, self._processed / path.name)
                diag("command_duplicate", command_id=command.id)
                continue
            batch.add(command.id)
            deliveries.append(Delivery(command, path))
        return deliveries

    def ack(self, delivery: Delivery) -> None:
        os.replace(delivery.path, self._processed / delivery.path.name)
        self._seen.add(delivery.command.id)
```

Docstring модуля: заменить фразу про этап 2 на «Команда подтверждается (`ack`) только после записи состояния runner: падение между `peek` и `ack` повторяет доставку, а не теряет команду».

**Step 4: Verify GREEN**

Run: `cd runner && uv run pytest tests/test_inbox.py -q`
Expected: PASS.

**Step 5: Gates** — отложены до Task 7 (сборка `session.py`).

---

### Task 6: Машина хода — `Committing`, `Halted`, `Commit`

Зависимость: после этой задачи `session.py` не собирается до Task 7. Запускайте только `tests/test_turn.py`; коммита нет.

**Files:**
- Modify: `runner/src/ctrunner/turn.py` (целиком, 134 строки)
- Test: `runner/tests/test_turn.py`

**Step 1: Write the failing test**

В `tests/test_turn.py`: добавить `CMD = MessageCmd("c1", "go")`; во всех `Working(` вставить `CMD` вторым аргументом (`Working("t1", CMD, frozenset(), 0, ())`); `Working(turn_id="t1", pending=…)` → `Working(turn_id="t1", cmd=CMD, pending=…)`; тип `s: Idle | Working` → `s: TurnState`; в `test_queued_command_starts_after_done` очередь `c2` берётся из состояния `Working("t1", CMD, frozenset(), 0, (MessageCmd("c2", "next"),))`. Импорты: добавить `Commit`, `Committing`, `Halted`, `TurnState`, `on_commit_failed`, `on_committed`.

Изменённые ожидания:

```python
def test_result_with_background_task_does_not_end_turn() -> None:
    s: TurnState = Working("t1", CMD, frozenset(), 0, ())
    s, _ = on_message(s, sysmsg("task_started", task_id="a", is_backgrounded=True))
    s, fx = on_message(s, res())
    assert isinstance(s, Working)
    assert fx == ()
    s, _ = on_message(s, sysmsg("task_notification", task_id="a"))
    s, fx = on_message(s, res())
    assert s == Committing("t1", CMD, ())
    assert fx == (Commit("t1"),)
    assert isinstance(s, Committing)
    assert on_committed(s) == (Idle(queue=()), (TurnDone("t1"),))


def test_queued_command_starts_after_commit() -> None:
    queued = MessageCmd("c2", "next")
    s, fx = on_message(Working("t1", CMD, frozenset(), 0, (queued,)), res())
    assert s == Committing("t1", CMD, (queued,))
    assert fx == (Commit("t1"),)
    assert isinstance(s, Committing)
    s, fx = on_committed(s)
    assert s == Idle(queue=(queued,))
    assert fx == (TurnDone("t1"),)
    assert isinstance(s, Idle)
    s2, fx2 = next_queued(s, turn_id="t2")
    assert s2 == Working("t2", queued, frozenset(), 0, ())
    assert fx2 == (SendPrompt("t2", "next"),)
```

(старый `test_queued_command_starts_after_done` удалить). Новые тесты:

```python
def test_commit_failure_fails_turn_without_completion() -> None:
    s = Committing("t1", CMD, (MessageCmd("c2", "next"),))
    assert on_commit_failed(s, "disk_full") == (
        Idle(queue=(MessageCmd("c2", "next"),)),
        (TurnFailedFx("t1", "disk_full"),),
    )


def test_command_during_commit_is_queued() -> None:
    s, fx = on_command(Committing("t1", CMD, ()), MessageCmd("c2", "next"), turn_id="t2")
    assert s == Committing("t1", CMD, (MessageCmd("c2", "next"),))
    assert fx == ()


def test_late_message_during_commit_is_ignored() -> None:
    s = Committing("t1", CMD, ())
    assert on_message(s, res()) == (s, ())


def test_halted_waits_for_human_then_runs_oldest_queued_first() -> None:
    older = MessageCmd("c0", "old")
    halted = Halted(queue=(older,))
    assert on_message(halted, res()) == (halted, ())
    s, fx = on_command(halted, MessageCmd("c2", "new"), turn_id="t9")
    assert s == Working("t9", older, frozenset(), 0, (MessageCmd("c2", "new"),))
    assert fx == (SendPrompt("t9", "old"),)


def test_phases() -> None:
    assert Committing("t1", CMD, ()).phase is Phase.WORKING
    assert Halted(queue=()).phase is Phase.FAILED
```

**Step 2: Verify RED**

Run: `cd runner && uv run pytest tests/test_turn.py -q`
Expected: FAIL: `ImportError: cannot import name 'Commit' from 'ctrunner.turn'`.

**Step 3: Implement** (`turn.py`)

- `Working` получает поле `cmd: MessageCmd` вторым (после `turn_id`).
- Новые состояния и эффект:

```python
@final
@dataclass(frozen=True, slots=True)
class Committing:
    """Результат получен, идёт git commit. Новые команды ждут в очереди."""

    turn_id: str
    cmd: MessageCmd
    queue: tuple[MessageCmd, ...]

    @property
    def phase(self) -> Phase:
        return Phase.WORKING


@final
@dataclass(frozen=True, slots=True)
class Halted:
    """Автопродолжения исчерпаны: runner жив, `health=crashed`, ждёт команду человека."""

    queue: tuple[MessageCmd, ...]

    @property
    def phase(self) -> Phase:
        return Phase.FAILED


type TurnState = Idle | Working | Committing | Halted


@final
@dataclass(frozen=True, slots=True)
class Commit:
    turn_id: str
```

- `type Effect = SendPrompt | Commit | TurnDone | TurnFailedFx`.
- `on_command`:

```python
def on_command(s: TurnState, cmd: MessageCmd, *, turn_id: str) -> Step:
    match s:
        case Idle():
            return next_queued(Idle((*s.queue, cmd)), turn_id=turn_id)
        case Halted():  # команда человека возвращает runner в работу
            return next_queued(Idle((*s.queue, cmd)), turn_id=turn_id)
        case Working() | Committing():
            return replace(s, queue=(*s.queue, cmd)), ()
        case _:
            assert_never(s)
```

- `next_queued`: `Working(turn_id, first, frozenset(), 0, tuple(rest))`.
- `on_message`:

```python
    match s:
        case Idle() | Halted() | Committing():
            # `result` фоновой доработки после хода или запоздавшее сообщение: журнал уже записан.
            return s, ()
        case Working():
            return _working_on_message(s, msg)
        case _:
            assert_never(s)
```

- В `_working_on_message` конец: `return Committing(s.turn_id, s.cmd, s.queue), (Commit(s.turn_id),)`.
- Новые функции:

```python
def on_committed(s: Committing) -> Step:
    return Idle(s.queue), (TurnDone(s.turn_id),)


def on_commit_failed(s: Committing, reason: str) -> Step:
    return Idle(s.queue), (TurnFailedFx(s.turn_id, reason),)
```

**Step 4: Verify GREEN**

Run: `cd runner && uv run pytest tests/test_turn.py -q`
Expected: PASS.

**Step 5: Gates** — отложены до Task 7.

---

### Task 7: `Session` — персистентность, commit, восстановление

**Files:**
- Modify: `runner/src/ctrunner/session.py:15-47` (импорты), `:55-62` (протоколы), `:79-134` (`__init__`, `run`), `:162-185` (`_read`, `_commands`), `:204-227` (`_apply`, `_start_queued`), `:311-312` (`_turn_id`) и новые методы
- Test: `runner/tests/test_session.py`

**Step 1: Write the failing test**

Подготовка `tests/test_session.py`:

```python
from collections.abc import Callable
from functools import partial

from ctrunner.checkpoint import CheckpointError, CheckpointFailure
from ctrunner.eventlog import kinds_of_turn
from ctrunner.protocol import MessageCmd
from ctrunner.recovery import Continue, Finalize, GiveUp, ResumeMode
from ctrunner.state import EMPTY, OpenTurn, PersistedState, load_state, save_state

SHA = "a" * 40


class FakeCommit:
    """Вместо git: записывает ходы, при `error` отказывает как диск."""

    def __init__(self) -> None:
        self.turns: list[str] = []
        self.error: CheckpointError | None = None

    def __call__(self, turn_id: str) -> str:
        if self.error is not None:
            raise self.error
        self.turns.append(turn_id)
        return SHA
```

`Env.__init__(self, root: Path, state: PersistedState = EMPTY)`: добавить

```python
        self.state_file = root / "state.json"
        self.commit = FakeCommit()
        self.saved: asyncio.Queue[tuple[PersistedState, int]] = asyncio.Queue()
```

и в `Session(...)`: `state=state, save_state=self._save, commit=self.commit, turn_kinds=lambda turn_id: kinds_of_turn(self.events, turn_id)`, где

```python
    def _save(self, state: PersistedState) -> None:
        save_state(self.state_file, state)
        pending = len(list(self.inbox_dir.glob("[!.]*.json")))  # файлов ещё не подтверждено
        self.saved.put_nowait((state, pending))

    async def wait_state(self, ready: Callable[[PersistedState, int], bool]) -> None:
        async with asyncio.timeout(5):
            while True:
                if ready(*await self.saved.get()):
                    return
```

Добавить фикстуру-фабрику (для тестов с начальным состоянием; существующая `env` остаётся):

```python
@pytest.fixture
def make_env(tmp_path: Path) -> Iterator[Callable[[PersistedState], Env]]:
    made: list[Env] = []

    def make(state: PersistedState) -> Env:
        e = Env(tmp_path, state)
        made.append(e)
        return e

    yield make
    for e in made:
        e.log.close()
```

Обновить ожидание в `test_turn_is_logged_and_stop_exits_zero`:
`env.kinds() == ["turn_started", "sdk", "sdk", "checkpoint", "turn_completed"]` и добавить проверки `env.commit.turns` (один `turn_id`) и `load_state(env.state_file)`: `open_turn is None`, `last_sha == SHA`, `session_id == "s1"`.

Новые тесты:

```python
async def test_open_turn_is_durable_before_query(env: Env) -> None:
    seen: list[PersistedState] = []

    class Spy(FakeClient):
        async def query(self, prompt: str) -> None:
            seen.append(load_state(env.state_file))
            await super().query(prompt)

    client = Spy([result()])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "go"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c2", "kind": "stop"})
    await run
    assert seen[0].open_turn is not None
    assert seen[0].open_turn.cmd == MessageCmd("c1", "go")


async def test_queued_command_is_durable_before_ack(env: Env) -> None:
    client = FakeClient([])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "a"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_STARTED)
    put(env.inbox_dir, {"id": "c2", "kind": "message", "text": "b"})
    observed: list[int] = []

    def has_c2(state: PersistedState, pending: int) -> bool:
        if state.queue != (MessageCmd("c2", "b"),):
            return False
        observed.append(pending)
        return True

    await env.wait_state(has_c2)
    assert observed == [1]  # файл c2 ещё в inbox в момент записи state
    put(env.inbox_dir, {"id": "c3", "kind": "stop"})
    assert await run is Exit.STOPPED
```

Сбои и восстановление:

```python
def open_state(count: int = 0) -> PersistedState:
    return PersistedState("sess-1", OpenTurn("t1", MessageCmd("c1", "собери отчёт")), None, count, ())


async def test_commit_failure_fails_turn_without_completion(env: Env) -> None:
    env.commit.error = CheckpointError(CheckpointFailure.DISK_FULL, "no space")
    client = FakeClient([result()])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "go"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_FAILED)
    assert env.events_of("turn_failed")[0]["payload"] == {"reason": "disk_full"}
    assert "turn_completed" not in env.kinds()
    assert "checkpoint" not in env.kinds()
    put(env.inbox_dir, {"id": "c2", "kind": "stop"})  # команды принимаются дальше
    assert await run is Exit.STOPPED
    assert load_state(env.state_file).open_turn is None


@pytest.mark.parametrize("mode", list(ResumeMode))
async def test_interrupted_turn_is_continued(
    make_env: Callable[[PersistedState], Env], mode: ResumeMode
) -> None:
    env = make_env(open_state(count=1))
    client = FakeClient([assistant("ok"), result()])
    step = Continue("t1", MessageCmd("c1", "собери отчёт"), 2, mode)
    run = asyncio.create_task(env.session.run(client, step))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c9", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert env.kinds()[:2] == ["turn_interrupted", "turn_resumed"]
    assert env.events_of("turn_resumed")[0]["payload"] == {"attempt": 2, "mode": mode.value}
    assert "собери отчёт" in client.prompts[0]
    assert env.kinds()[-2:] == ["checkpoint", "turn_completed"]
    state = load_state(env.state_file)
    assert state.open_turn is None
    assert state.autoresume_count == 0


async def test_attempt_is_durable_before_query(make_env: Callable[[PersistedState], Env]) -> None:
    env = make_env(open_state(count=0))
    seen: list[PersistedState] = []

    class Spy(FakeClient):
        async def query(self, prompt: str) -> None:
            seen.append(load_state(env.state_file))
            await super().query(prompt)

    client = Spy([result()])
    step = Continue("t1", MessageCmd("c1", "x"), 1, ResumeMode.RESUME)
    run = asyncio.create_task(env.session.run(client, step))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c9", "kind": "stop"})
    await run
    assert seen[0].autoresume_count == 1
    assert seen[0].open_turn is not None


async def test_finalize_adds_only_missing_events(make_env: Callable[[PersistedState], Env]) -> None:
    env = make_env(open_state())
    env.log.append(EventKind.TURN_STARTED, "t1", {"prompt": "x"})
    env.log.append(EventKind.CHECKPOINT, "t1", {"sha": SHA})
    client = FakeClient([])
    run = asyncio.create_task(env.session.run(client, Finalize("t1", SHA)))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c9", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert env.kinds() == ["turn_started", "checkpoint", "turn_completed"]
    assert client.prompts == []  # ход не повторяется
    state = load_state(env.state_file)
    assert state.open_turn is None
    assert state.last_sha == SHA


async def test_give_up_fails_turn_then_human_command_resumes_work(
    make_env: Callable[[PersistedState], Env],
) -> None:
    env = make_env(open_state(count=3))
    client = FakeClient([result()])
    run = asyncio.create_task(env.session.run(client, GiveUp("t1")))
    await env.wait_kind(EventKind.TURN_FAILED)
    assert env.kinds() == ["turn_interrupted", "turn_failed"]
    assert env.events_of("turn_failed")[0]["payload"] == {"reason": "resume_limit"}
    assert client.prompts == []
    put(env.inbox_dir, {"id": "c2", "kind": "message", "text": "ещё раз"})
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c3", "kind": "stop"})
    assert await run is Exit.STOPPED


async def test_redelivered_command_is_not_run_twice(
    make_env: Callable[[PersistedState], Env],
) -> None:
    env = make_env(open_state())
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "собери отчёт"})  # ack не успел
    client = FakeClient([result()])
    step = Continue("t1", MessageCmd("c1", "собери отчёт"), 1, ResumeMode.RESUME)
    run = asyncio.create_task(env.session.run(client, step))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c9", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert len(client.prompts) == 1  # только продолжение, без второго запуска c1
    assert len(env.events_of("turn_started")) == 0
```

Примечание: `Exit` уже импортирован в шапке файла; `pytest` тоже.

**Step 2: Verify RED**

Run: `cd runner && uv run pytest tests/test_session.py -q`
Expected: FAIL: `TypeError: Session.__init__() got an unexpected keyword argument 'state'`.

**Step 3: Implement** (`session.py`)

Импорты: `from ctrunner.checkpoint import CheckpointError`; `from ctrunner.recovery import CLEAN, Clean, Continue, Finalize, GiveUp, Recovery, continuation_prompt`; `from ctrunner.sdkevents import normalize, session_id_of, to_json`; `from ctrunner.state import OpenTurn, PersistedState, is_session_id`; из `ctrunner.turn` добавить `Commit`, `Committing`, `Halted`, `on_commit_failed`, `on_committed`.

`Session.__init__`: новые обязательные keyword-параметры
`state: PersistedState`, `save_state: Callable[[PersistedState], None]`, `commit: Callable[[str], str]`, `turn_kinds: Callable[[str], frozenset[EventKind]]`; поля:

```python
        self._saved = state
        self._save_state = save_state
        self._commit_turn = commit
        self._turn_kinds = turn_kinds
        self._session_id = state.session_id
        self._last_sha = state.last_sha
        self._autoresume = state.autoresume_count
        self._state: TurnState = Idle(queue=state.queue)
```

`run(self, client, recovery: Recovery = CLEAN)`: первой строкой `await self._recover(client, recovery)` (до создания задач; ошибка уходит в `crash` процессной границей `main.run`).

Новые методы:

```python
    async def _recover(self, client: SdkClient, recovery: Recovery) -> None:
        match recovery:
            case Clean():
                await self._start_queued(client)
            case Finalize(turn_id=turn_id, sha=sha):
                logged = self._turn_kinds(turn_id)
                if EventKind.CHECKPOINT not in logged:
                    self._log.append(EventKind.CHECKPOINT, turn_id, {"sha": sha})
                if EventKind.TURN_COMPLETED not in logged:
                    self._log.append(EventKind.TURN_COMPLETED, turn_id, {})
                self._last_sha = sha
                self._save()
                await self._start_queued(client)
            case Continue() as step:
                self._touch()
                self._autoresume = step.attempt
                self._state = Working(step.turn_id, step.cmd, frozenset(), 0, self._state.queue)
                self._save()  # открытый ход и счётчик попыток durable до query
                self._log.append(EventKind.TURN_INTERRUPTED, step.turn_id, {})
                attempt: dict[str, JsonValue] = {"attempt": step.attempt, "mode": step.mode.value}
                self._log.append(EventKind.TURN_RESUMED, step.turn_id, attempt)
                await client.query(continuation_prompt(step))
            case GiveUp(turn_id=turn_id):
                self._log.append(EventKind.TURN_INTERRUPTED, turn_id, {})
                self._log.append(EventKind.TURN_FAILED, turn_id, {"reason": "resume_limit"})
                self._state = Halted(self._state.queue)
                self._save()
            case _:
                assert_never(recovery)

    def _snapshot(self) -> PersistedState:
        open_turn = _open_turn(self._state)
        return PersistedState(
            session_id=self._session_id,
            open_turn=open_turn,
            last_sha=self._last_sha,
            autoresume_count=self._autoresume if open_turn is not None else 0,
            queue=self._state.queue,
        )

    def _save(self) -> None:
        """Пишет только изменившееся: потоковые сообщения не должны давать fsync на каждый токен."""
        snapshot = self._snapshot()
        if snapshot != self._saved:
            self._save_state(snapshot)
            self._saved = snapshot

    def _known_ids(self) -> frozenset[str]:
        open_turn = _open_turn(self._state)
        open_ids = {open_turn.cmd.id} if open_turn is not None else set()
        return frozenset(open_ids | {c.id for c in self._state.queue})

    def _committing(self) -> Committing:
        if not isinstance(self._state, Committing):
            raise RuntimeError(f"expected Committing, got {type(self._state).__name__}")
        return self._state

    async def _commit(self, client: SdkClient, turn_id: str) -> None:
        self._touch()
        try:
            sha = await asyncio.to_thread(self._commit_turn, turn_id)
        except CheckpointError as error:
            diag("checkpoint_failed", reason=error.failure.value, detail=self._redactor.text(error.detail))
            self._state, effects = on_commit_failed(self._committing(), error.failure.value)
        else:
            self._log.append(EventKind.CHECKPOINT, turn_id, {"sha": sha})
            self._last_sha = sha
            self._state, effects = on_committed(self._committing())
        await self._apply(client, effects)
```

Модульная функция:

```python
def _open_turn(state: TurnState) -> OpenTurn | None:
    match state:
        case Working() | Committing():
            return OpenTurn(state.turn_id, state.cmd)
        case Idle() | Halted():
            return None
        case _:
            assert_never(state)
```

Изменения существующих методов:

- `_read`: после `normalize`/записи события — `sid = session_id_of(msg)`; `if sid is not None and is_session_id(sid): self._session_id = sid`; после `await self._apply(client, effects)` — `self._save()`.
- `_commands`: `for delivery in await asyncio.to_thread(self._inbox.peek): cmd = delivery.command`; `if cmd.id in self._known_ids(): self._inbox.ack(delivery); continue`; ветка `MessageCmd()`:

```python
                    case MessageCmd():
                        self._state, effects = on_command(self._state, cmd, turn_id=_new_id())
                        self._save()  # команда durable (в очереди или как открытый ход) до ack
                        self._inbox.ack(delivery)
                        await self._apply(client, effects)
```

  ветки `ForkAnswerCmd()` и `StopCmd()`: `self._inbox.ack(delivery)` перед действием.
- `_apply`: добавить/изменить ветки

```python
                case SendPrompt(turn_id=turn_id, text=text):
                    self._touch()
                    self._save()  # открытый ход durable до query
                    self._log.append(EventKind.TURN_STARTED, turn_id, {"prompt": text})
                    await client.query(text)
                case Commit(turn_id=turn_id):
                    await self._commit(client, turn_id)
                case TurnDone(turn_id=turn_id):
                    self._log.append(EventKind.TURN_COMPLETED, turn_id, {})
                    self._save()  # state закрывается после событий
                    await self._start_queued(client)
                case TurnFailedFx(turn_id=turn_id, reason=reason):
                    self._log.append(EventKind.TURN_FAILED, turn_id, {"reason": reason})
                    self._save()
                    await self._start_queued(client)
```

- `_turn_id`: `return self._state.turn_id if isinstance(self._state, Working | Committing) else None`.
- Комментарий в шапке модуля («блокировки не нужны») дополнить: «`to_thread` для git commit допустим, потому что на это время состояние `Committing` не принимает новых ходов».

**Step 4: Verify GREEN**

Run: `cd runner && uv run pytest tests/test_session.py tests/test_turn.py tests/test_inbox.py -q`
Expected: PASS.

**Step 5: Refactor и gates (после задач 5–7)**

Run: `cd runner && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests evals && uv run pytest -q`
Expected: PASS без новых предупреждений. Если ruff форматирует длинные строки — `uv run ruff format .` и повторить.

**Step 6: Commit** — `git add -A && git commit -m "feat(runner): persisted turn state, git checkpoint and recovery in session (Tasks 5-7)"`

---

### Task 8: Сборка в `main` — состояние, план, `resume`

**Files:**
- Modify: `runner/src/ctrunner/main.py` (`run`, новые функции, константы)
- Test: `runner/tests/test_main_recovery.py`

**Step 1: Write the failing test**

```python
import json
from pathlib import Path

from ctrunner.eventlog import EventLog
from ctrunner.main import EX_STATE, halt_on_corrupt_state, resumable_session
from ctrunner.protocol import EventKind
from ctrunner.redact import Redactor
from ctrunner.state import EMPTY, PersistedState, StateCorrupt

SID = "749df2fe-1b50-4c46-92f3-e2cdf0ce322d"


def with_session(session_id: str | None) -> PersistedState:
    return PersistedState(session_id, None, None, 0, ())


def test_resumable_when_transcript_exists(tmp_path: Path) -> None:
    transcript = tmp_path / "projects" / "-workspace-project" / f"{SID}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    assert resumable_session(with_session(SID), tmp_path) == SID


def test_not_resumable_without_transcript_or_session(tmp_path: Path) -> None:
    assert resumable_session(with_session(SID), tmp_path) is None
    assert resumable_session(EMPTY, tmp_path) is None


def test_corrupt_state_is_logged_and_exits_with_state_code(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with EventLog.open(path, Redactor.of([]), lambda: 1.0) as log:
        assert halt_on_corrupt_state(log, StateCorrupt("bad")) == EX_STATE
    row = json.loads(path.read_text().splitlines()[0])
    assert row["kind"] == EventKind.TURN_FAILED
    assert row["payload"] == {"reason": "state_corrupt"}
```

**Step 2: Verify RED**

Run: `cd runner && uv run pytest tests/test_main_recovery.py -q`
Expected: FAIL: `ImportError: cannot import name 'EX_STATE'`.

**Step 3: Implement**

Константы: `STATE_FILE: Final = "state.json"`, `EX_STATE: Final = 65  # sysexits EX_DATAERR: state.json повреждён, рестарт не поможет`.

```python
def resumable_session(state: PersistedState, claude_dir: Path) -> str | None:
    """Сессию можно продолжить, только если на томе есть её транскрипт."""
    session_id = state.session_id
    if session_id is None or not any(claude_dir.glob(f"projects/*/{session_id}.jsonl")):
        return None
    return session_id


def halt_on_corrupt_state(log: EventLog, error: StateCorrupt) -> int:
    diag("state_corrupt", detail=str(error))
    log.append(EventKind.TURN_FAILED, None, {"reason": "state_corrupt"})
    return EX_STATE
```

`run(cfg)` после `log.append(SESSION_STARTED…)`:

```python
        events = cfg.runner_dir / "events.jsonl"  # вынести выше и использовать в EventLog.open
        state_file = cfg.runner_dir / STATE_FILE
        try:
            state = load_state(state_file)
        except StateCorrupt as error:
            return halt_on_corrupt_state(log, error)
        clear_stale_lock(cfg.project_dir)
        resumable = resumable_session(state, cfg.claude_dir)
        recovery = plan(state, read_head(cfg.project_dir), resumable=resumable is not None)
        diag("recovery_planned", plan=type(recovery).__name__)
        session = Session(
            ...,  # прежние аргументы
            state=replace(state, session_id=resumable),
            save_state=partial(save_state, state_file),
            commit=partial(commit_turn, cfg.project_dir),
            turn_kinds=partial(kinds_of_turn, events),
        )
```

`ClaudeAgentOptions(..., resume=resumable)`; `outcome = await session.run(client, recovery)`. Импорты: `dataclasses.replace`, `functools.partial`, из `checkpoint` — `clear_stale_lock, commit_turn, read_head, run_git`; `eventlog.kinds_of_turn`; `recovery.plan`; `state.PersistedState, StateCorrupt, load_state, save_state`.

Условные шаги по результатам Task 0:
- (3) «ошибка при connect»: если `async with ClaudeSDKClient(...)` с `resume` падает до `session.run`, повторить с `resume=None`, `recovery = plan(replace(state, session_id=None)…, resumable=False)` и пересобрать `Session`; логировать `diag("resume_failed")`. Не добавляйте фолбэк, если спайк показал иное поведение.
- (4) «resume после оборванного вызова недопустим»: `resumable_session` возвращает `None` всегда; тест `test_resumable_when_transcript_exists` переписать на этот контракт.

**Step 4: Verify GREEN**

Run: `cd runner && uv run pytest tests/test_main_recovery.py tests/test_main_exit.py tests/test_seed.py -q`
Expected: PASS.

**Step 5: Gates**

Run: `cd runner && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests evals && uv run pytest -q`
Expected: PASS без новых предупреждений.

**Step 6: Commit** — `git add -A && git commit -m "feat(runner): wire state, recovery plan and resume into runner startup (Task 8)"`

---

### Task 9: E2E `test_restart` (`docker kill` посреди хода)

Нужны Docker, образ и окружение шлюза; стоит десятки тысяч токенов (~3–5 мин).

**Files:**
- Modify: `runner/evals/conftest.py:46-70` (`Project.wait`, `Project.head`)
- Create: `runner/evals/test_restart.py`

**Step 1: Write the failing test** (`evals/test_restart.py`)

```python
from collections.abc import Callable

import pytest

from evals.conftest import Project, docker, sessions

PROMPT = (
    "Создай файл a.txt с текстом one. Затем выполни `sleep 90`. "
    "Затем создай файл b.txt с текстом two."
)


@pytest.mark.e2e
def test_restart_mid_turn_resumes(start_project: Callable[..., Project]) -> None:
    project = start_project()
    project.wait("healthy", lambda: project.docker_health() == "healthy", 90)
    before = project.head()
    sessions("send", project.id, PROMPT)
    project.wait_event("sdk", 120, count=3)  # ход идёт
    docker("kill", project.container)
    project.wait_event("turn_resumed", 180)
    project.wait_event("turn_completed", 300)
    events = project.events()
    kinds = [e["kind"] for e in events]
    assert kinds.index("turn_interrupted") < kinds.index("turn_resumed") < kinds.index("turn_completed")
    checkpoint = next(e for e in events if e["kind"] == "checkpoint")
    assert isinstance(checkpoint["payload"], dict)
    assert checkpoint["payload"]["sha"] == project.head() != before
    assert docker("exec", project.container, "cat", "/workspace/project/b.txt").strip() == "two"
```

**Step 2: Verify RED**

Run: `cd runner && docker build -t coreteams-runner:dev . && source ~/.config/brotherhood/env.zsh && uv run pytest evals/test_restart.py -m e2e -q`
Expected: FAIL: `AttributeError: 'Project' object has no attribute 'head'`.

**Step 3: Implement** (`evals/conftest.py`)

В `Project`:

```python
    def head(self) -> str:
        return docker(
            "exec", self.container, "git", "-C", "/workspace/project", "rev-parse", "HEAD"
        ).strip()
```

В `Project.wait` обернуть `ready()`: контейнер между kill и рестартом не отвечает на `docker exec`, это «ещё не готово», а не ошибка теста:

```python
        def ready_or_down() -> bool:
            try:
                return ready()
            except subprocess.CalledProcessError:
                return False

        deadline = time.monotonic() + timeout_s
        while not ready_or_down():
            ...
```

**Step 4: Verify GREEN**

Run: `cd runner && source ~/.config/brotherhood/env.zsh && uv run pytest evals -m e2e -q`
Expected: PASS для `test_restart` и прежних `test_start`, `test_isolation`, `test_stall`. Флак: если kill пришёлся до первого tool call, тест всё равно валиден (ход открыт); если падает по таймауту `turn_resumed`, смотреть `sessions logs <id>` и `docker inspect --format '{{.RestartCount}}'`.

**Step 5: Gates**

Run: `cd runner && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests evals`
Expected: PASS.

**Step 6: Commit** — `git add -A && git commit -m "test(runner): e2e restart mid-turn resumes and checkpoints (Task 9)"`

---

### Task 10: Документация

**Files:**
- Modify: `runner/README.md:60-118` (команды, ограничения, таблица evals)
- Modify: `docs/2.4/protocol.md:95-100` (события этапа 2) и раздел «Гарантии доставки»
- Modify: `docs/plans/2026-09-25-runner-design.md` (раздел «Этапы»)

**Step 1: README**

- Удалить пункт «Нет checkpoint и resume: …» из «Ограничения этапа 1», заменить на «Нет гейтвея…» (оставить) и добавить раздел `## Восстановление` с таблицей исходов (`turn_completed` по `Turn-Id`, `turn_interrupted` → `turn_resumed{mode}`, `turn_failed{resume_limit}`), описанием `state.json` (`/workspace/.runner/state.json`, агенту недоступен), правилом «после `resume_limit` runner жив, `docker ps` показывает unhealthy, новая команда `sessions send` возвращает его в работу», кодом выхода 65 (`state.json` повреждён: починить или удалить файл вручную, потеряв информацию о прерванном ходе) и пометкой для режима `fresh`.
- Таблица evals: строка `test_restart` — «`docker kill` посреди хода → `turn_interrupted` → `turn_resumed` → `turn_completed`, новый `checkpoint`».
- Таблица кодов выхода: добавить `65`.

**Step 2: protocol.md**

- `turn_interrupted`: `{}` — оставить; `turn_resumed`: `{"attempt", "mode": "resume" | "fresh"}`; `turn_failed`: `"reason"` расширить `"checkpoint_failed" | "resume_limit" | "state_corrupt"`.
- «Гарантии доставки», пункт про команды: «доставка — at-least-once до `ack`, исполнение идемпотентно по `id`: файл переносится в `processed/` после записи состояния».

**Step 3: Дизайн runner**

В «Этап 2» отметить: `checkpoint, resume, автопродолжение, eval_restart` — выполнено (ссылка на `2026-10-01-runner-recovery-design.md`); WS-клиент и повтор событий остаются.

**Step 4: Verify**

Run: `cd /Users/romanov/PycharmProjects/PP-Sber-Autumn-2026 && grep -rn "Нет checkpoint" runner docs; git diff --stat`
Expected: `grep` пуст; изменены только перечисленные файлы.

**Step 5: Commit** — `git add -A && git commit -m "docs(2.4): recovery protocol, README and design status (Task 10)"`

---

### Task 11: Итоговая проверка и MR

**Step 1: Полные gates**

Run: `cd runner && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests evals && uv run pytest -q`
Expected: PASS без новых предупреждений.

**Step 2: Второй checkpoint python-rules** — по `git diff master...HEAD` пройти таблицу ссылок (`main`, `developer`, `idioms`, `errors-common`, `legacy-errors`, `testing`) и исправить найденное.

**Step 3: Обязательные обзоры** — выполнить `security-review` и `reliability-review`; в ответе исполнителя — разделы `## Security Review` и `## Reliability Review`. Проверить явно: (а) `state.json` и `events.jsonl` недоступны агенту (сторож, `.runner/` вне корней); (б) `session_id` из `state.json` валидируется до подстановки в glob; (в) хуки git отключены для checkpoint; (г) подделка trailer ограничена собственным ходом; (д) нет `await` между чтением и записью состояния без `Committing`; (е) порядок записи commit → события → state и state → `query` → ack подтверждён тестами `test_open_turn_is_durable_before_query`, `test_queued_command_is_durable_before_ack`, `test_finalize_adds_only_missing_events`; (ж) падение записи `state.json` приводит к `crash` (exit 1), а не к тихой потере.

**Step 4: Push и MR** (пользователь просил новый MR)

Run: `git push -u origin PPS-116-runner-recovery`
Run: `gh pr create --base master --head PPS-116-runner-recovery --title "feat(runner): восстановление хода после рестарта (PPS-116, PPS-133)" --body-file <файл с описанием>`
Описание: что сделано (checkpoint, `state.json`, resume/fresh, автопродолжение ≤3, двухфазный inbox), как проверено (unit + `test_restart`), ограничения (WS и повтор событий — MR #4), строка `Closes PPS-116, PPS-133`.
Expected: URL MR; затем `mcp__ccd_pr__get_status` / `bind_pr` по правилам приложения.
