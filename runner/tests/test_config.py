from dataclasses import replace
from pathlib import Path
from traceback import format_exception

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
        load_config({**ENV, "GATEWAY_URL": "ws://gw:8000/runner"}, secrets)


def test_gateway_loaded_and_token_hidden(secrets: Path) -> None:
    (secrets / "gateway_token").write_text("gw-secret-token\n")
    cfg = load_config({**ENV, "GATEWAY_URL": "ws://gw:8000/runner"}, secrets)
    assert cfg.gateway_url == "ws://gw:8000/runner"
    assert cfg.gateway_token == "gw-secret-token"
    assert "gw-secret" not in repr(cfg)


def test_gateway_url_must_be_websocket(secrets: Path) -> None:
    (secrets / "gateway_token").write_text("gw-secret-token\n")
    with pytest.raises(ConfigError, match="GATEWAY_URL"):
        load_config({**ENV, "GATEWAY_URL": "http://gw:8000"}, secrets)


@pytest.mark.parametrize(
    ("key", "field", "scheme"),
    [
        ("ANTHROPIC_BASE_URL", "anthropic_base_url", "https"),
        ("GATEWAY_URL", "gateway_url", "wss"),
    ],
)
@pytest.mark.parametrize(
    "authority",
    [
        "user:private-password@example.test",
        "private-password@example.test",
        "user:private-password@[broken",
        "[private-password]",
        "example.test:private-password",
        "example.test:65536",
        "example.test:-1",
        "[::1]unexpected:8000",
        "backend[::1]:8000",
        "example.test/api\nprivate-password",
        "example.test/api#section",
    ],
)
def test_endpoint_rejects_unsafe_authority_without_leaking(
    secrets: Path, key: str, field: str, scheme: str, authority: str,
) -> None:
    (secrets / "gateway_token").write_text("gw-secret-token\n")
    url = f"{scheme}://{authority}/runner"
    good = load_config({**ENV, "GATEWAY_URL": "ws://backend:8000/v1/runner"}, secrets)
    with pytest.raises(ConfigError, match=key) as loaded:
        load_config({**ENV, key: url}, secrets)
    with pytest.raises(ConfigError, match=key) as constructed:
        replace(good, **{field: url})
    for failure in (loaded, constructed):
        diagnostic = "".join(format_exception(failure.value))
        assert url not in diagnostic
        assert "private-password" not in diagnostic


@pytest.mark.parametrize(
    ("key", "url"),
    [
        ("ANTHROPIC_BASE_URL", "http://proxy:8317"),
        ("ANTHROPIC_BASE_URL", "https://proxy.example.test/api"),
        ("GATEWAY_URL", "ws://backend:8000/v1/runner"),
        ("GATEWAY_URL", "ws://localhost:8000/runner"),
        ("GATEWAY_URL", "ws://host.docker.internal:8000/runner"),
        ("GATEWAY_URL", "ws://[::1]:8000/runner"),
        ("GATEWAY_URL", "wss://gateway.example.test/runner"),
    ],
)
def test_endpoint_preserves_supported_transport(secrets: Path, key: str, url: str) -> None:
    (secrets / "gateway_token").write_text("gw-secret-token\n")
    cfg = load_config({**ENV, key: url}, secrets)
    assert (cfg.gateway_url if key == "GATEWAY_URL" else cfg.anthropic_base_url) == url


def test_gateway_fragment_is_configuration_error(secrets: Path) -> None:
    (secrets / "gateway_token").write_text("gw-secret-token\n")
    with pytest.raises(ConfigError, match="GATEWAY_URL"):
        load_config({**ENV, "GATEWAY_URL": "ws://backend:8000/v1/runner#section"}, secrets)


@pytest.mark.parametrize("token", ["short", "gw secret token"])
def test_gateway_token_must_be_a_single_long_token(secrets: Path, token: str) -> None:
    (secrets / "gateway_token").write_text(token + "\n")
    with pytest.raises(ConfigError, match="gateway_token"):
        load_config({**ENV, "GATEWAY_URL": "ws://gw:8000/runner"}, secrets)
