import pytest

from ctrunner.protocol import JsonValue
from ctrunner.redact import Redactor


def test_redacts_nested_strings() -> None:
    r = Redactor.of(["sk-SECRET-123"])
    value: JsonValue = {"a": ["x sk-SECRET-123 y", {"b": "sk-SECRET-123"}], "n": 5, "z": None}
    assert r.apply(value) == {"a": ["x *** y", {"b": "***"}], "n": 5, "z": None}


def test_redacts_dict_keys() -> None:
    assert Redactor.of(["k3y-long-secret"]).apply({"k3y-long-secret": 1}) == {"***": 1}


def test_longest_secret_first() -> None:
    assert Redactor.of(["abcdefgh", "abcdefghijk"]).apply("abcdefghijk") == "***"


def test_no_secrets_is_identity() -> None:
    assert Redactor.of([]).apply({"a": "b"}) == {"a": "b"}


@pytest.mark.parametrize("short", ["", "short"])
def test_rejects_short_secrets(short: str) -> None:
    with pytest.raises(ValueError, match="secret too short"):
        Redactor.of([short])
