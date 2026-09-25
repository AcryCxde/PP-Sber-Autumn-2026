"""Точка входа runner: предполётные проверки, подготовка проекта, сборка сессии SDK."""

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, Protocol, assert_never

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

from ctrunner import __version__
from ctrunner.config import HEALTH_FILE, TOKEN_FILE, ConfigError, RunnerConfig, load_config
from ctrunner.diag import diag
from ctrunner.eventlog import EventLog
from ctrunner.guard import Policy
from ctrunner.inbox import Inbox
from ctrunner.probe import ProbeResult, probe
from ctrunner.protocol import EventKind
from ctrunner.redact import Redactor
from ctrunner.session import Session

EX_CONFIG: Final = 78  # sysexits: ошибка конфигурации, рестарт не поможет
EX_UNAVAILABLE: Final = 1  # сеть или SDK: Docker перезапустит
PROBE_TIMEOUT_S: Final = 30.0
# Docker перезапускает on-failure с задержкой от 100 мс: 5 рестартов проходят за секунды,
# поэтому кратковременную недоступность cliproxy переживаем внутри процесса.
PROBE_RETRY_DELAYS_S: Final = (5.0, 10.0, 20.0, 40.0, 60.0)
GIT_TIMEOUT_S: Final = 600.0  # git add большого проекта при первом старте
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
        _git(project, "init", "-q")
    if _git(project, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
        _git(project, "add", "-A")
        _git(project, "commit", "-q", "--allow-empty", "-m", "init")


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


def _git(project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 — аргументы фиксированы, без shell
        ["git", "-C", str(project), *args],  # noqa: S607 — git из PATH образа
        check=check,
        capture_output=True,
        timeout=GIT_TIMEOUT_S,
    )


async def run(cfg: RunnerConfig) -> int:
    await asyncio.to_thread(seed_project, cfg.project_dir, seed=SEED_DIR, ctf=CTF_DIR)
    for directory in (cfg.runner_dir, cfg.claude_dir):
        directory.mkdir(parents=True, exist_ok=True)
    redactor = Redactor.of([cfg.anthropic_token])
    with EventLog.open(cfg.runner_dir / "events.jsonl", redactor, time.time) as log:
        log.append(
            EventKind.SESSION_STARTED,
            None,
            {"project_id": cfg.project_id, "runner_version": __version__},
        )
        session = Session(
            log=log,
            redactor=redactor,
            inbox=Inbox(cfg.runner_dir / "inbox"),
            policy=Policy(roots=(cfg.project_dir, TMP_DIR), bash_system=BASH_SYSTEM),
            health_file=HEALTH_FILE,
            stall_after_s=cfg.stall_after_s,
        )
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, session.request_stop)
        options = ClaudeAgentOptions(
            cwd=cfg.project_dir,
            setting_sources=["project"],
            env=cfg.sdk_env(),
            include_partial_messages=True,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[session.pre_tool_use])]},
            can_use_tool=session.can_use_tool,
            stderr=session.sdk_stderr,
        )
        try:
            async with ClaudeSDKClient(options=options) as client:
                outcome = await session.run(client)
        except Exception as error:  # процессная граница: запись отказа и выход 1
            return session.crash(error)
        return outcome


def cli() -> None:
    cfg = preflight_with_retry(os.environ, secrets_dir=SECRETS_DIR, probe_fn=probe)
    if isinstance(cfg, int):
        sys.exit(cfg)
    sys.exit(asyncio.run(run(cfg)))
