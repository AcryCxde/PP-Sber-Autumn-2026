"""Shared lifecycle preserves the pinned public fold on incomplete/replayed histories."""

import importlib.util
import json
import random
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from ctrunner.harness_diagnostic import DiagnosticFeed
from ctrunner.v0 import EventType, Translator, parse_row


def original_translator():
    path = Path(__file__).parent / "journals" / "translator_f945365.py.txt"
    loader = SourceFileLoader("pinned_f945365_translator", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module.Translator


def test_public_and_diagnostic_liveness_match_pinned_fold(tmp_path):
    pinned = original_translator()
    randomizer = random.Random(914)  # noqa: S311 — reproducible synthetic histories, not secrets
    choices = [
        ("turn_started", {}), ("turn_completed", {}),
        ("turn_failed", {"reason": "result_error"}),
        ("turn_failed", {"reason": "result_error", "fatal": True}),
        ("turn_failed", {"reason": "sdk_crashed"}),
        ("turn_failed", {"reason": "state_corrupt"}),
        ("sdk", {"type": "result", "session_id": "s", "total_cost_usd": 0.25}),
        ("sdk", {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "synthetic"}]}}),
    ]
    path = tmp_path / "events.jsonl"
    for _ in range(100):
        old, current = pinned("p"), Translator("p")
        rows, public_usage = [], []
        for seq in range(1, 151):
            kind, payload = randomizer.choice(choices)
            row = {"seq": seq, "ts": seq, "turn_id": randomizer.choice([None, "a", "b", "c"]),
                   "kind": kind, "payload": payload}
            rows.append(row)
            event = parse_row(row)
            before, after = old.feed(event), current.feed(event)
            assert [(d.event_id, d.run_id, d.type.value, d.payload) for d in before] == [
                (d.event_id, d.run_id, d.type.value, d.payload) for d in after]
            public_usage.extend(d.event_id for d in after if d.type is EventType.USAGE_REPORTED)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        feed = DiagnosticFeed(path, "p", "/workspace/project")
        frames = []
        while not feed.caught_up():
            frames.extend(feed.poll(19))
        assert [frame["source_event_id"] for frame in frames] == public_usage


@pytest.mark.parametrize("payload", [None, [], {}, {"reason": None}, {"reason": ""},
                                    {"reason": 1}, {"reason": "unknown"},
                                    {"reason": "result_error", "fatal": True}])
def test_failed_boundary_matches_pinned_exception_and_drafts(payload):
    pinned = original_translator()("p")
    current = Translator("p")
    start = parse_row({"seq": 1, "ts": 1, "turn_id": "r", "kind": "turn_started", "payload": {}})
    failed = parse_row({"seq": 2, "ts": 2, "turn_id": "r", "kind": "turn_failed",
                        "payload": payload})
    pinned.feed(start)
    current.feed(start)
    try:
        expected = pinned.feed(failed)
    except ValueError as error:
        with pytest.raises(type(error)) as actual:
            current.feed(failed)
        assert str(actual.value) == str(error)
    else:
        assert [(d.event_id, d.type.value, d.payload) for d in current.feed(failed)] == [
            (d.event_id, d.type.value, d.payload) for d in expected]
