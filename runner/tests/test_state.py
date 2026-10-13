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
    to_json,
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
        json.dumps(
            {
                "v": 1,
                "session_id": "bad id!",
                "open_turn": None,
                "last_sha": None,
                "autoresume_count": 0,
                "queue": [],
            }
        ),
        json.dumps(
            {
                "v": 1,
                "session_id": None,
                "open_turn": None,
                "last_sha": None,
                "autoresume_count": -1,
                "queue": [],
            }
        ),
        json.dumps(  # счётчик без открытого хода
            {
                "v": 1,
                "session_id": None,
                "open_turn": None,
                "last_sha": None,
                "autoresume_count": 1,
                "queue": [],
            }
        ),
        json.dumps(
            {
                "v": 1,
                "session_id": None,
                "open_turn": None,
                "last_sha": None,
                "autoresume_count": 0,
                "queue": [{"id": "c"}],
            }
        ),
    ],
)
def test_corrupt_state_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(StateCorrupt):
        load_state(path)


def test_parse_state_accepts_what_save_writes() -> None:
    assert parse_state(to_json(FULL)) == FULL


def test_session_id_shape() -> None:
    assert is_session_id("749df2fe-1b50-4c46-92f3-e2cdf0ce322d")
    assert not is_session_id("../x")
    assert not is_session_id("")
