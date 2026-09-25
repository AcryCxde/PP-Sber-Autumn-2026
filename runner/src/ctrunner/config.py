"""Граница конфигурации: env и /run/secrets → RunnerConfig. Ошибка называет ключ, не значение."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, final
from urllib.parse import urlsplit

from ctrunner.redact import MIN_SECRET_LEN

PROJECT_ID_RE: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
MODEL_ENV: Final = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)
OPTIONAL_MODEL_ENV: Final = ("ANTHROPIC_DEFAULT_FABLE_MODEL",)
TOKEN_FILE: Final = "anthropic_token"  # noqa: S105 — имя файла, а не значение
DEFAULT_STALL_AFTER_S: Final = 720.0
DEFAULT_WORKSPACE: Final = "/workspace"
# Вне корней сторожа: в /tmp агент мог бы подделать собственный статус живости.
HEALTH_FILE: Final = Path("/run/ctrunner/health.json")


class ConfigError(Exception):
    """Конфигурация или секрет отсутствуют либо некорректны — перезапуск не поможет."""


@final
@dataclass(frozen=True, slots=True)
class RunnerConfig:
    project_id: str
    anthropic_base_url: str
    anthropic_token: str = field(repr=False)
    models: Mapping[str, str]
    stall_after_s: float
    workspace: Path

    @property
    def project_dir(self) -> Path:
        return self.workspace / "project"

    @property
    def runner_dir(self) -> Path:
        return self.workspace / ".runner"

    @property
    def claude_dir(self) -> Path:
        return self.workspace / "claude"

    @property
    def probe_model(self) -> str:
        return self.models["ANTHROPIC_DEFAULT_HAIKU_MODEL"]

    def sdk_env(self) -> dict[str, str]:
        return {
            "ANTHROPIC_BASE_URL": self.anthropic_base_url,
            "ANTHROPIC_AUTH_TOKEN": self.anthropic_token,
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            **self.models,
        }


def load_config(env: Mapping[str, str], secrets_dir: Path) -> RunnerConfig:
    return RunnerConfig(
        project_id=_project_id(env),
        anthropic_base_url=_base_url(env),
        anthropic_token=_token(secrets_dir),
        models=_models(env),
        stall_after_s=_stall_after_s(env),
        workspace=_workspace(env),
    )


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if not value:
        raise ConfigError(f"env {key} missing")
    return value


def _project_id(env: Mapping[str, str]) -> str:
    value = _required(env, "PROJECT_ID")
    if not PROJECT_ID_RE.fullmatch(value):
        raise ConfigError("env PROJECT_ID must match [a-z0-9][a-z0-9-]{0,39}")
    return value


def _base_url(env: Mapping[str, str]) -> str:
    value = _required(env, "ANTHROPIC_BASE_URL")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("env ANTHROPIC_BASE_URL must be an http(s) URL")
    return value.rstrip("/")


def _models(env: Mapping[str, str]) -> dict[str, str]:
    required = {key: _required(env, key) for key in MODEL_ENV}
    optional = {key: env[key] for key in OPTIONAL_MODEL_ENV if env.get(key)}
    return required | optional


def _stall_after_s(env: Mapping[str, str]) -> float:
    raw = env.get("STALL_AFTER_S", "")
    if not raw:
        return DEFAULT_STALL_AFTER_S
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigError("env STALL_AFTER_S must be a number") from error
    if not math.isfinite(value) or value <= 0:
        raise ConfigError("env STALL_AFTER_S must be a positive finite number")
    return value


def _workspace(env: Mapping[str, str]) -> Path:
    value = Path(env.get("WORKSPACE") or DEFAULT_WORKSPACE)
    if not value.is_absolute():
        raise ConfigError("env WORKSPACE must be an absolute path")
    return value


def _token(secrets_dir: Path) -> str:
    try:
        value = (secrets_dir / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise ConfigError(f"secret {TOKEN_FILE} missing") from error
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"secret {TOKEN_FILE} unreadable") from error
    if not value:
        raise ConfigError(f"secret {TOKEN_FILE} missing")
    if len(value) < MIN_SECRET_LEN:
        raise ConfigError(f"secret {TOKEN_FILE} too short")
    if any(c.isspace() for c in value):
        raise ConfigError(f"secret {TOKEN_FILE} must be a single token without whitespace")
    return value
