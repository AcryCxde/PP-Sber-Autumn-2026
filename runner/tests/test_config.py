from dataclasses import replace
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


def test_sdk_env(secrets: Path) -> None:
    env = load_config(ENV, secrets).sdk_env()
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test-0123456789"
    assert env["ANTHROPIC_BASE_URL"] == "http://proxy:8317"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-5.6-luna"
    assert env["CLAUDE_CONFIG_DIR"] == "/workspace/claude"


def test_repr_hides_token(secrets: Path) -> None:
    assert "sk-test" not in repr(load_config(ENV, secrets))


def test_missing_secret(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="anthropic_token"):
        load_config(ENV, tmp_path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PROJECT_ID", "../x"),
        ("PROJECT_ID", ""),
        ("STALL_AFTER_S", "0"),
        ("STALL_AFTER_S", "abc"),
        ("STALL_AFTER_S", "nan"),
        ("STALL_AFTER_S", "inf"),
        ("WORKSPACE", "relative/dir"),
        ("ANTHROPIC_BASE_URL", "ftp://x"),
    ],
)
def test_invalid_env(secrets: Path, key: str, value: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config({**ENV, key: value}, secrets)


def test_short_token_is_config_error(tmp_path: Path) -> None:
    (tmp_path / "anthropic_token").write_text("short\n")
    with pytest.raises(ConfigError, match="anthropic_token"):
        load_config(ENV, tmp_path)


def test_missing_model_env(secrets: Path) -> None:
    env = {k: v for k, v in ENV.items() if k != "ANTHROPIC_DEFAULT_HAIKU_MODEL"}
    with pytest.raises(ConfigError, match="ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        load_config(env, secrets)


def test_error_never_contains_value(secrets: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_config({**ENV, "ANTHROPIC_BASE_URL": "ftp://user:hunter2@x"}, secrets)
    assert "hunter2" not in str(info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "../x"),
        ("anthropic_base_url", "ftp://x"),
        ("anthropic_token", "short"),
        ("stall_after_s", float("inf")),
        ("workspace", Path("rel")),
    ],
)
def test_direct_construction_keeps_invariants(secrets: Path, field: str, value: object) -> None:
    good = load_config(ENV, secrets)
    with pytest.raises(ConfigError):
        replace(good, **{field: value})  # type: ignore[arg-type]


def test_gateway_is_optional(secrets: Path) -> None:
    cfg = load_config(ENV, secrets)
    assert cfg.gateway_url is None
    assert cfg.gateway_token is None


def test_gateway_url_requires_secret(secrets: Path) -> None:
    with pytest.raises(ConfigError, match="gateway_token"):
        load_config({**ENV, "GATEWAY_URL": "wss://gw:8000/runner"}, secrets)


def test_gateway_loaded_and_token_hidden(secrets: Path) -> None:
    (secrets / "gateway_token").write_text("gw-secret-token\n")
    cfg = load_config({**ENV, "GATEWAY_URL": "wss://gw:8000/runner"}, secrets)
    assert cfg.gateway_url == "wss://gw:8000/runner"
    assert cfg.gateway_token == "gw-secret-token"
    assert "gw-secret" not in repr(cfg)


def test_gateway_url_must_be_websocket(secrets: Path) -> None:
    (secrets / "gateway_token").write_text("gw-secret-token\n")
    with pytest.raises(ConfigError, match="GATEWAY_URL"):
        load_config({**ENV, "GATEWAY_URL": "http://gw:8000"}, secrets)


@pytest.mark.parametrize(
    "url",
    [
        "ws://127.0.0.1:8000/runner",
        "ws://[::1]:8000/runner",
        "ws://localhost:8000/runner",
        "ws://host.docker.internal:8000/runner",  # во что sessions превращает localhost
        "wss://gw.example/runner",
    ],
)
def test_gateway_url_allows_wss_and_loopback_ws(secrets: Path, url: str) -> None:
    (secrets / "gateway_token").write_text("gw-secret-token\n")
    assert load_config({**ENV, "GATEWAY_URL": url}, secrets).gateway_url == url


@pytest.mark.parametrize(
    "url",
    [
        "ws://gw:8000/runner",
        "ws://10.0.0.5/runner",
        "ws://localhost.evil.example/runner",
        "ws://127.0.0.1.evil.example/runner",
        "wss://user:pass-word-1@gw.example/runner",
        "wss://user@gw.example/runner",
        "ws://user:pass-word-1@127.0.0.1:8000/runner",
    ],
)
def test_gateway_url_rejects_plain_ws_remote_and_userinfo(secrets: Path, url: str) -> None:
    (secrets / "gateway_token").write_text("gw-secret-token\n")
    with pytest.raises(ConfigError, match="GATEWAY_URL") as exc:
        load_config({**ENV, "GATEWAY_URL": url}, secrets)
    assert "pass-word-1" not in str(exc.value)
    assert url not in str(exc.value)


@pytest.mark.parametrize("token", ["short", "gw secret token"])
def test_gateway_token_must_be_a_single_long_token(secrets: Path, token: str) -> None:
    (secrets / "gateway_token").write_text(token + "\n")
    with pytest.raises(ConfigError, match="gateway_token"):
        load_config({**ENV, "GATEWAY_URL": "wss://gw:8000/runner"}, secrets)
