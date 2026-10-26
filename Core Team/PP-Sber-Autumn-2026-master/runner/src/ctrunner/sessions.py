"""CLI-оркестратор `sessions` поверх docker CLI: один контейнер `ct-<id>` на проект."""

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, final
from urllib.parse import urlsplit

from ctrunner.config import PROJECT_ID_RE

DEFAULT_IMAGE: Final = "coreteams-runner:dev"
SECRETS_ROOT: Final = Path.home() / ".config" / "coreteams" / "secrets"
TOKEN_FILE: Final = "anthropic_token"  # noqa: S105 — имя файла, а не значение
EVENTS: Final = "/workspace/.runner/events.jsonl"
HEALTH: Final = "/run/ctrunner/health.json"
# Что из окружения хоста уходит в контейнер: адрес шлюза и выбор моделей. Ключи — только файлом.
ENV_ALLOW: Final = re.compile(
    r"ANTHROPIC_BASE_URL|ANTHROPIC_DEFAULT_[A-Z]+_MODEL(_NAME)?"
    r"|CLAUDE_CODE_(SUBAGENT_MODEL|AUTO_COMPACT_WINDOW|ENABLE_GATEWAY_MODEL_DISCOVERY)"
)
ENV_DENY: Final = re.compile(r"TOKEN|KEY|SECRET|PASSWORD")
LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})


@final
@dataclass(frozen=True, slots=True)
class RunSpec:
    project_id: str
    image: str
    secrets_dir: Path
    data_dir: Path | None
    env: Mapping[str, str]
    stall_after_s: float

    def __post_init__(self) -> None:
        if not PROJECT_ID_RE.fullmatch(self.project_id):
            raise ValueError("project_id must match [a-z0-9][a-z0-9-]{0,39}")


def container_env(host_env: Mapping[str, str]) -> dict[str, str]:
    env = {k: v for k, v in host_env.items() if ENV_ALLOW.fullmatch(k) and not ENV_DENY.search(k)}
    if "ANTHROPIC_BASE_URL" in env:
        env["ANTHROPIC_BASE_URL"] = _reachable_from_container(env["ANTHROPIC_BASE_URL"])
    return env


def _reachable_from_container(url: str) -> str:
    parts = urlsplit(url)
    if parts.hostname not in LOCAL_HOSTS:
        return url
    port = f":{parts.port}" if parts.port else ""
    return parts._replace(netloc=f"host.docker.internal{port}").geturl()


def run_args(spec: RunSpec) -> list[str]:
    pid = spec.project_id
    seed = ["-v", f"{spec.data_dir}:/seed:ro"] if spec.data_dir is not None else []
    env = [a for k, v in spec.env.items() for a in ("-e", f"{k}={v}")]
    return [
        "docker", "run", "-d", "--name", f"ct-{pid}",
        "--label", f"coreteams.project={pid}",
        # --init: PID 1 собирает зомби от Bash-процессов агента, иначе упрёмся в --pids-limit.
        "--init", "--restart", "on-failure:5", "--user", "agent",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", "1g", "--cpus", "1", "--pids-limit", "256",
        "--tmpfs", "/tmp", "--tmpfs", "/home/agent:uid=1000",  # noqa: S108 — tmpfs, не файл
        "--tmpfs", "/run/ctrunner:uid=1000",
        "-v", f"proj-{pid}:/workspace", "-v", f"{spec.secrets_dir}:/run/secrets:ro", *seed,
        "-e", f"PROJECT_ID={pid}", "-e", f"STALL_AFTER_S={spec.stall_after_s:g}", *env,
        spec.image,
    ]  # fmt: skip


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — argv без shell, id проверен
        ["docker", *args],  # noqa: S607 — docker из PATH
        check=check,
        text=True,
    )


def _secrets_init(project_id: str) -> None:
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    if not token:
        sys.exit("sessions: ANTHROPIC_AUTH_TOKEN is not set (source the gateway env first)")
    directory = SECRETS_ROOT / project_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / TOKEN_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    path.chmod(0o600)
    print(path)


def _start(project_id: str, data: Path | None, stall_after_s: float, image: str) -> None:
    secrets = SECRETS_ROOT / project_id
    if not (secrets / TOKEN_FILE).is_file():
        sys.exit(f"sessions: run `sessions secrets init {project_id}` first")
    spec = RunSpec(
        project_id=project_id,
        image=image,
        secrets_dir=secrets,
        data_dir=data.resolve() if data is not None else None,
        env=container_env(os.environ),
        stall_after_s=stall_after_s,
    )
    _docker("volume", "create", "--label", f"coreteams.project={project_id}", f"proj-{project_id}")
    _docker(*run_args(spec)[1:])


def _status(project_id: str) -> None:
    fmt = "state={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}"
    _docker("inspect", "--format", fmt, f"ct-{project_id}")
    _docker("exec", f"ct-{project_id}", "cat", HEALTH, check=False)
    print()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sessions")
    sub = parser.add_subparsers(dest="cmd", required=True)
    secrets = sub.add_parser("secrets").add_subparsers(dest="secrets_cmd", required=True)
    secrets.add_parser("init").add_argument("project_id")
    start = sub.add_parser("start")
    start.add_argument("project_id")
    start.add_argument("--data", type=Path)
    start.add_argument("--stall-after", type=float, default=720.0)
    start.add_argument("--image", default=DEFAULT_IMAGE)
    send = sub.add_parser("send")
    send.add_argument("project_id")
    send.add_argument("text")
    answer = sub.add_parser("answer")
    answer.add_argument("project_id")
    answer.add_argument("fork_id")
    answer.add_argument("labels", nargs="+")
    logs = sub.add_parser("logs")
    logs.add_argument("project_id")
    logs.add_argument("-f", "--follow", action="store_true")
    for name in ("status", "stop", "restart"):
        sub.add_parser(name).add_argument("project_id")
    rm = sub.add_parser("rm")
    rm.add_argument("project_id")
    rm.add_argument("--volume", action="store_true")
    return parser


def cli() -> None:
    args = _parser().parse_args()
    project_id: str = args.project_id
    if not PROJECT_ID_RE.fullmatch(project_id):
        sys.exit("sessions: project_id must match [a-z0-9][a-z0-9-]{0,39}")
    name = f"ct-{project_id}"
    try:
        match args.cmd:
            case "secrets":
                _secrets_init(project_id)
            case "start":
                _start(project_id, args.data, args.stall_after, args.image)
            case "send":
                _docker("exec", name, "ctrunner-inbox", "message", args.text)
            case "answer":
                _docker("exec", name, "ctrunner-inbox", "fork-answer", args.fork_id, *args.labels)
            case "status":
                _status(project_id)
            case "logs":
                follow = ["-f"] if args.follow else []
                _docker("exec", name, "tail", *follow, "-n", "+1", EVENTS)
            case "stop" | "restart":
                _docker(args.cmd, name)
            case "rm":
                _docker("rm", "-f", name)
                if args.volume:
                    _docker("volume", "rm", f"proj-{project_id}")
            case other:
                sys.exit(f"sessions: unknown command {other}")
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
