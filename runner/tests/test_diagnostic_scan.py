"""Full scan pages catch up cooperatively; polling waits only at the tail/window."""

import asyncio
import json
from pathlib import Path

import pytest

from ctrunner.gateway import DiagnosticAcks, GatewayClient
from ctrunner.harness_diagnostic import DiagnosticReceipts


class PollWaitReached(Exception):
    pass


class ObservedWait(asyncio.Event):
    async def wait(self):
        raise PollWaitReached


@pytest.mark.parametrize("draining", [False, True])
async def test_non_result_history_does_not_wait_per_page(tmp_path: Path, monkeypatch, draining):
    path = tmp_path / "events.jsonl"
    with path.open("w") as stream:
        for seq in range(1, 100_001):
            stream.write(json.dumps({"seq": seq, "ts": seq, "kind": "sdk",
                                     "turn_id": "r", "payload": {
                                         "type": "assistant", "content": "synthetic"}}) + "\n")
        stream.write(json.dumps({"seq": 100_001, "ts": 100_001,
                                 "kind": "turn_started", "turn_id": "r", "payload": {}}) + "\n")
        stream.write(json.dumps({"seq": 100_002, "ts": 100_002, "kind": "sdk",
                                 "turn_id": "r", "payload": {"type": "result",
                                     "session_id": "synthetic", "total_cost_usd": 1}}) + "\n")
    client = GatewayClient(
        url="ws://unused", token="test", project_id="p", runner_version="test",
        session_id=None, journal=path, health_file=tmp_path / "health",
        inbox_dir=tmp_path / "inbox", project_root="/workspace/project",
        last_seq=lambda: 100_002, fork_questions=lambda _: None,
    )
    client._draining = ObservedWait()
    if draining:
        client.drain()
    sent = []
    zero_yields = 0
    original_sleep = asyncio.sleep

    async def observed_sleep(delay):
        nonlocal zero_yields
        if delay:
            raise PollWaitReached
        zero_yields += 1
        await original_sleep(0)

    class Socket:
        async def send(self, raw):
            sent.append(json.loads(raw))

    monkeypatch.setattr(asyncio, "sleep", observed_sleep)
    with pytest.raises(PollWaitReached):
        await client._diagnostic_loop(Socket(), DiagnosticAcks(), DiagnosticReceipts(path))
    assert len(sent) == 1
    assert sent[0]["source_journal_seq"] == 100_002
    assert zero_yields >= 390


async def test_full_diagnostic_window_waits_instead_of_spinning(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    rows = [{"seq": 1, "ts": 1, "kind": "turn_started", "turn_id": "r", "payload": {}}]
    rows.extend({"seq": seq, "ts": seq, "kind": "sdk", "turn_id": "r", "payload": {
        "type": "result", "session_id": "s", "total_cost_usd": 1}}
        for seq in range(2, 302))
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    client = GatewayClient(
        url="ws://unused", token="test", project_id="p", runner_version="test",
        session_id=None, journal=path, health_file=tmp_path / "health",
        inbox_dir=tmp_path / "inbox", project_root="/workspace/project",
        last_seq=lambda: 301, fork_questions=lambda _: None,
    )
    client._draining = ObservedWait()
    sent = []

    class Socket:
        async def send(self, raw):
            sent.append(json.loads(raw))

    with pytest.raises(PollWaitReached):
        await client._diagnostic_loop(Socket(), DiagnosticAcks(), DiagnosticReceipts(path))
    assert len(sent) == 256
