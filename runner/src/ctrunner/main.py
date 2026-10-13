"""Точка входа runner: предполётные проверки, подготовка проекта, сборка сессии SDK."""

import asyncio
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Final, Protocol, assert_never

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ProcessError

from ctrunner import __version__
from ctrunner.checkpoint import Head, clear_stale_lock, commit_turn, read_head, run_git
from ctrunner.config import HEALTH_FILE, TOKEN_FILE, ConfigError, RunnerConfig, load_config
from ctrunner.diag import diag
from ctrunner.eventlog import EventLog, kinds_of_turn
from ctrunner.guard import Policy
from ctrunner.inbox import Inbox
from ctrunner.probe import ProbeResult, probe
from ctrunner.protocol import EventKind
from ctrunner.recovery import CLEAN, Recovery, plan
from ctrunner.redact import Redactor
from ctrunner.session import EventSink, SdkClient, Session
from ctrunner.state import PersistedState, StateCorrupt, load_state, save_state

EX_CONFIG: Final = 78  # sysexits: ошибка конфигурации, рестарт не поможет
EX_STATE: Final = 65  # sysexits EX_DATAERR: state.json повреждён, рестарт не поможет
EX_UNAVAILABLE: Final = 1  # сеть или SDK: Docker перезапустит
PROBE_TIMEOUT_S: Final = 30.0
# Docker перезапускает on-failure с задержкой от 100 мс: 5 рестартов проходят за секунды,
# поэтому кратковременную недоступность cliproxy переживаем внутри процесса.
PROBE_RETRY_DELAYS_S: Final = (5.0, 10.0, 20.0, 40.0, 60.0)
STATE_FILE: Final = "state.json"
SECRETS_DIR: Final = Path("/run/secrets")
SEED_DIR: Final = Path("/seed")
CTF_DIR: Final = Path("/opt/ctf/.claude")
TMP_DIR: Final = Path("/tmp")  # noqa: S108 — корень сторожа, а не временный файл
BASH_SYSTEM: Final = (Path("/usr"), Path("/bin"), Path("/lib"), Path("/dev/null"))


class ProbeFn(Protocol):
    def __call__(
        self, base_url: str, token: str, model: str, *, timeout_s: float
    ) -> ProbeResult: ...


def preflight(
    env: Mapping[str, str], *, secrets_dir: Path, probe_fn: ProbeFn
) -> RunnerConfig | int:
    """Конфиг и ключ до `hello`: без секрета до сети дело не доходит."""
    try:
        cfg = load_config(env, secrets_dir)
    except ConfigError as error:
        diag("config_error", detail=str(error))
        return EX_CONFIG
    verdict = probe_fn(
        cfg.anthropic_base_url, cfg.anthropic_token, cfg.probe_model, timeout_s=PROBE_TIMEOUT_S
    )
    match verdict:
        case ProbeResult.OK:
            return cfg
        case ProbeResult.REJECTED:
            diag("secret_rejected", file=TOKEN_FILE)
            return EX_CONFIG
        case ProbeResult.UNREACHABLE:
            diag("proxy_unreachable", base_url=cfg.anthropic_base_url)
            return EX_UNAVAILABLE
        case _:
            assert_never(verdict)


def preflight_with_retry(
    env: Mapping[str, str],
    *,
    secrets_dir: Path,
    probe_fn: ProbeFn,
    delays: Sequence[float] = PROBE_RETRY_DELAYS_S,
    sleep: Callable[[float], None] = time.sleep,
) -> RunnerConfig | int:
    """Повторяется только недоступность сети; ошибка конфигурации и отказ ключа — сразу."""
    outcome = preflight(env, secrets_dir=secrets_dir, probe_fn=probe_fn)
    for attempt, delay in enumerate(delays, start=1):
        if outcome != EX_UNAVAILABLE:
            break
        diag("proxy_retry", attempt=attempt, delay_s=delay)
        sleep(delay)
        outcome = preflight(env, secrets_dir=secrets_dir, probe_fn=probe_fn)
    return outcome


def seed_project(project: Path, *, seed: Path, ctf: Path) -> None:
    """Идемпотентно: каждый шаг проверяет результат, копии ставятся на место одним rename."""
    if not project.exists():
        _copy_into_place(seed if seed.is_dir() else None, project)
    if not (project / ".claude").exists() and ctf.is_dir():
        _copy_into_place(ctf, project / ".claude")
    if not (project / ".git").exists():
        run_git(project, "init", "-q")
    if run_git(project, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
        run_git(project, "add", "-A")
        run_git(project, "commit", "-q", "--allow-empty", "-m", "init")


def _copy_into_place(source: Path | None, target: Path) -> None:
    # Оборванная копия не должна выглядеть готовой при следующем старте.
    staging = target.with_name(f".{target.name}.partial")
    shutil.rmtree(staging, ignore_errors=True)
    if source is None:
        staging.mkdir(parents=True)
    else:
        staging.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, staging, symlinks=True)
    os.replace(staging, target)


def resumable_session(state: PersistedState, claude_dir: Path) -> str | None:
    """Сессию можно продолжить, только если на томе есть её транскрипт."""
    session_id = state.session_id
    if session_id is None or not any(claude_dir.glob(f"projects/*/{session_id}.jsonl")):
        return None
    return session_id


def halt_on_corrupt_state(log: EventSink, error: StateCorrupt) -> int:
    diag("state_corrupt", detail=str(error))
    log.append(EventKind.TURN_FAILED, None, {"reason": "state_corrupt"})
    return EX_STATE


class Launchable(Protocol):
    """Что `run_with_resume` требует от сессии: запуск и запись отказа."""

    async def run(self, client: SdkClient, recovery: Recovery = CLEAN) -> int: ...

    def crash(self, error: BaseException | None) -> int: ...


async def run_with_resume[S: Launchable](  # noqa: PLR0913 — зависимости передаются явно
    state: PersistedState,
    head: Head,
    resumable: str | None,
    *,
    build_session: Callable[[PersistedState], S],
    open_client: Callable[[S, str | None], AbstractAsyncContextManager[SdkClient]],
    redact: Callable[[str], str],
) -> int:
    """Запускает сессию; если `resume` не подключился, один раз начинает с чистой сессии.

    Resume несуществующей сессии падает при `connect` (`ProcessError`), а не подменяется новой
    молча. Сессия одноразовая, поэтому для повтора собирается новая под состояние без
    `session_id`, а план пересчитывается как для режима `fresh`. Ошибка после подключения и
    вторая неудача — обычный crash.
    """
    resume = resumable
    while True:
        session_state = replace(state, session_id=resume)
        recovery = plan(session_state, head, resumable=resume is not None)
        diag("recovery_planned", plan=type(recovery).__name__, resume=resume is not None)
        session = build_session(session_state)
        connected = False
        try:
            async with open_client(session, resume) as client:
                connected = True
                return await session.run(client, recovery)
        except ProcessError as error:
            if connected or resume is None:
                return session.crash(error)
            diag("resume_failed", session_id=resume, detail=redact(str(error)))
            resume = None
        except Exception as error:  # процессная граница: запись отказа и выход 1
            return session.crash(error)


async def run(cfg: RunnerConfig) -> int:
    await asyncio.to_thread(seed_project, cfg.project_dir, seed=SEED_DIR, ctf=CTF_DIR)
    for directory in (cfg.runner_dir, cfg.claude_dir):
        directory.mkdir(parents=True, exist_ok=True)
    redactor = Redactor.of([cfg.anthropic_token])
    events = cfg.runner_dir / "events.jsonl"
    state_file = cfg.runner_dir / STATE_FILE
    with EventLog.open(events, redactor, time.time) as log:
        log.append(
            EventKind.SESSION_STARTED,
            None,
            {"project_id": cfg.project_id, "runner_version": __version__},
        )
        try:
            state = load_state(state_file)
        except StateCorrupt as error:
            return halt_on_corrupt_state(log, error)
        clear_stale_lock(cfg.project_dir)

        def build_session(session_state: PersistedState) -> Session:
            session = Session(
                log=log,
                redactor=redactor,
                inbox=Inbox(cfg.runner_dir / "inbox"),
                policy=Policy(roots=(cfg.project_dir, TMP_DIR), bash_system=BASH_SYSTEM),
                health_file=HEALTH_FILE,
                stall_after_s=cfg.stall_after_s,
                state=session_state,
                save_state=partial(save_state, state_file),
                commit=partial(commit_turn, cfg.project_dir),
                turn_kinds=partial(kinds_of_turn, events),
            )
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, session.request_stop)
            return session

        def open_client(session: Session, resume: str | None) -> ClaudeSDKClient:
            options = ClaudeAgentOptions(
                cwd=cfg.project_dir,
                setting_sources=["project"],
                env=cfg.sdk_env(),
                include_partial_messages=True,
                hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[session.pre_tool_use])]},
                can_use_tool=session.can_use_tool,
                stderr=session.sdk_stderr,
                resume=resume,
            )
            return ClaudeSDKClient(options=options)

        return await run_with_resume(
            state,
            read_head(cfg.project_dir),
            resumable_session(state, cfg.claude_dir),
            build_session=build_session,
            open_client=open_client,
            redact=redactor.text,
        )


def cli() -> None:
    cfg = preflight_with_retry(os.environ, secrets_dir=SECRETS_DIR, probe_fn=probe)
    if isinstance(cfg, int):
        sys.exit(cfg)
    sys.exit(asyncio.run(run(cfg)))
