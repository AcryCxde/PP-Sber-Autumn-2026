from pathlib import Path

import pytest

from ctrunner.sessions import RunSpec, container_env, run_args


def spec(**overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "project_id": "demo-a",
        "image": "coreteams-runner:dev",
        "secrets_dir": Path("/s/demo-a"),
        "data_dir": Path("/d/demo-a"),
        "env": {"ANTHROPIC_BASE_URL": "http://host.docker.internal:8317"},
        "stall_after_s": 60,
    }
    return RunSpec(**{**fields, **overrides})  # type: ignore[arg-type]


def test_run_args_hardening() -> None:
    args = run_args(spec())
    for flag in [
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "--pids-limit",
        "256",
        "--restart",
        "on-failure:5",
        "--init",
    ]:
        assert flag in args
    assert "proj-demo-a:/workspace" in args
    assert "/s/demo-a:/run/secrets:ro" in args
    assert "/d/demo-a:/seed:ro" in args
    assert "STALL_AFTER_S=60" in args
    assert "PROJECT_ID=demo-a" in args
    for tmpfs in ["/tmp", "/home/agent:uid=1000", "/run/ctrunner:uid=1000"]:
        assert tmpfs in args
    assert not any("TOKEN" in a for a in args)  # секреты только файлом
    assert args[-1] == "coreteams-runner:dev"


def test_run_args_without_data() -> None:
    assert not any(a.endswith(":/seed:ro") for a in run_args(spec(data_dir=None)))


def test_rejects_bad_project_id() -> None:
    with pytest.raises(ValueError, match="project_id"):
        spec(project_id="../x")


def test_container_env_allowlist_and_localhost_rewrite() -> None:
    host = {
        "ANTHROPIC_BASE_URL": "http://localhost:8317",
        "ANTHROPIC_AUTH_TOKEN": "sk-real-secret-000",
        "OPENAI_API_KEY": "sk-other-000000",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "m-opus",
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "Opus",
        "CLAUDE_CODE_SUBAGENT_MODEL": "m-sub",
        "PATH": "/usr/bin",
    }
    assert container_env(host) == {
        "ANTHROPIC_BASE_URL": "http://host.docker.internal:8317",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "m-opus",
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "Opus",
        "CLAUDE_CODE_SUBAGENT_MODEL": "m-sub",
    }


def test_container_env_keeps_remote_host() -> None:
    env = container_env({"ANTHROPIC_BASE_URL": "https://proxy.example:443"})
    assert env == {"ANTHROPIC_BASE_URL": "https://proxy.example:443"}
