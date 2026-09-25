from pathlib import Path

import pytest

from ctrunner.main import EX_CONFIG, EX_UNAVAILABLE, preflight
from ctrunner.probe import ProbeResult

ENV = {
    "PROJECT_ID": "demo-a",
    "ANTHROPIC_BASE_URL": "http://proxy:8317",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-5.6-sol",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-5.6-terra",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-5.6-luna",
}
TOKEN = "sk-test-0123456789"


def probe_must_not_run(base_url: str, token: str, model: str, *, timeout_s: float) -> ProbeResult:
    raise AssertionError("без секрета до сети дело не доходит")


def rejected(base_url: str, token: str, model: str, *, timeout_s: float) -> ProbeResult:
    return ProbeResult.REJECTED


def unreachable(base_url: str, token: str, model: str, *, timeout_s: float) -> ProbeResult:
    return ProbeResult.UNREACHABLE


def test_missing_secret_exits_78_before_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert preflight(ENV, secrets_dir=tmp_path, probe_fn=probe_must_not_run) == EX_CONFIG
    assert "secret anthropic_token missing" in capsys.readouterr().err


def test_rejected_key_exits_78(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "anthropic_token").write_text(TOKEN)
    assert preflight(ENV, secrets_dir=tmp_path, probe_fn=rejected) == EX_CONFIG
    assert TOKEN not in capsys.readouterr().err


def test_unreachable_proxy_exits_1(tmp_path: Path) -> None:
    (tmp_path / "anthropic_token").write_text(TOKEN)
    assert preflight(ENV, secrets_dir=tmp_path, probe_fn=unreachable) == EX_UNAVAILABLE == 1
