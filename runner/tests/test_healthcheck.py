import json
from pathlib import Path

import pytest

from ctrunner.healthcheck import verdict


@pytest.mark.parametrize(
    ("doc", "now", "code"),
    [
        ({"health": "ok", "written_at": 100.0}, 110.0, 0),
        ({"health": "ok", "written_at": 100.0}, 131.0, 1),  # heartbeat протух
        ({"health": "stalled", "written_at": 100.0}, 101.0, 1),
        ({"health": "crashed", "written_at": 100.0}, 101.0, 1),
        ({"health": "ok", "written_at": "100"}, 101.0, 1),  # неверный тип
        ({"health": "ok"}, 101.0, 1),
        ([1, 2], 101.0, 1),
    ],
)
def test_verdict(tmp_path: Path, doc: object, now: float, code: int) -> None:
    p = tmp_path / "h.json"
    p.write_text(json.dumps(doc))
    assert verdict(p, now=now) == code


def test_missing_or_broken_file(tmp_path: Path) -> None:
    assert verdict(tmp_path / "none.json", now=0.0) == 1
    (tmp_path / "b.json").write_text("{")
    assert verdict(tmp_path / "b.json", now=0.0) == 1
