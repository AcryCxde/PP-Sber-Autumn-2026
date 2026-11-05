"""`artifact.created`: размер и sha256 файла на момент `commit_sha` (если runner их записал)."""

from ctrunner.protocol import JsonValue
from ctrunner.v0 import Draft, EventType, parse_row, translate

PROJECT = "demo-curriculum"
SHA256 = "a" * 64


def rows(*raw: dict[str, JsonValue]) -> list[Draft]:
    return translate(PROJECT, [parse_row(r) for r in raw])


def row(seq: int, kind: str, payload: JsonValue) -> dict[str, JsonValue]:
    return {"seq": seq, "ts": 1791273600.0 + seq, "turn_id": "t1", "kind": kind, "payload": payload}


def write_two_files() -> list[dict[str, JsonValue]]:
    blocks: list[JsonValue] = [
        {"id": "w1", "name": "Write", "input": {"file_path": "/workspace/project/a.md"}},
        {"id": "w2", "name": "Write", "input": {"file_path": "/workspace/project/b.md"}},
    ]
    msg: dict[str, JsonValue] = {"model": "m", "content": blocks}
    results: list[JsonValue] = [
        {"tool_use_id": "w1", "content": "ok", "is_error": False},
        {"tool_use_id": "w2", "content": "ok", "is_error": False},
    ]
    return [
        row(1, "turn_started", {"prompt": "p"}),
        row(2, "sdk", {"type": "assistant", "parent_tool_use_id": None, "message": msg}),
        row(
            3, "sdk", {"type": "user", "parent_tool_use_id": None, "message": {"content": results}}
        ),
    ]


def artifacts(checkpoint: JsonValue) -> list[Draft]:
    drafts = rows(*write_two_files(), row(4, "checkpoint", checkpoint))
    return [d for d in drafts if d.type is EventType.ARTIFACT_CREATED]


def test_artifact_carries_size_and_sha256_of_its_file() -> None:
    files: JsonValue = {
        "a.md": {"size_bytes": 12, "sha256": SHA256},
        "b.md": {"size_bytes": 0, "sha256": "b" * 64},
    }
    first, second = artifacts({"sha": "abc", "files": files})
    assert first.payload["path"] == "a.md"
    assert first.payload["size_bytes"] == 12
    assert first.payload["sha256"] == SHA256
    assert second.payload["size_bytes"] == 0  # пустой файл — это факт, а не отсутствие
    assert second.payload["sha256"] == "b" * 64


def test_artifact_without_facts_has_no_such_fields_and_is_not_lost() -> None:
    checkpoints: list[JsonValue] = [{"sha": "abc"}, {"sha": "abc", "files": {}}]
    for checkpoint in checkpoints:
        first, second = artifacts(checkpoint)
        assert [d.payload["path"] for d in (first, second)] == ["a.md", "b.md"]
        assert "size_bytes" not in first.payload
        assert "sha256" not in first.payload


def test_facts_cover_only_some_files() -> None:
    first, second = artifacts(
        {"sha": "abc", "files": {"b.md": {"size_bytes": 3, "sha256": SHA256}}}
    )
    assert "sha256" not in first.payload
    assert second.payload["size_bytes"] == 3


def test_malformed_facts_are_dropped_not_zeroed() -> None:
    bad: list[JsonValue] = [
        {"size_bytes": -1, "sha256": SHA256},
        {"size_bytes": True, "sha256": SHA256},
        {"size_bytes": 3, "sha256": "XYZ"},
        {"size_bytes": 3, "sha256": "A" * 64},
        {"size_bytes": 3},
        {"sha256": SHA256},
        "мусор",
    ]
    for fact in bad:
        first, _ = artifacts({"sha": "abc", "files": {"a.md": fact}})
        assert "size_bytes" not in first.payload, fact
        assert "sha256" not in first.payload, fact


def test_non_object_files_field_is_ignored() -> None:
    first, _ = artifacts({"sha": "abc", "files": ["a.md"]})
    assert first.payload["path"] == "a.md"
    assert "sha256" not in first.payload


def test_event_ids_do_not_depend_on_facts() -> None:
    with_facts = artifacts({"sha": "abc", "files": {"a.md": {"size_bytes": 1, "sha256": SHA256}}})
    without = artifacts({"sha": "abc"})
    assert [d.event_id for d in with_facts] == [d.event_id for d in without]
    assert [d.payload["artifact_id"] for d in with_facts] == [
        d.payload["artifact_id"] for d in without
    ]
