"""Shutdown flushes the final public and optional private observations together."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from ctrunner.gateway import GatewayClient
from ctrunner.harness_diagnostic import SCHEMA, DiagnosticReceipts


@pytest.mark.parametrize("acknowledge", [True, False])
@pytest.mark.parametrize("capability", [True, False])
async def test_drain_reads_final_result_and_bounds_missing_ack(
    tmp_path: Path, acknowledge, capability,
):
    journal = tmp_path / "events.jsonl"
    journal.write_text("")
    connected = asyncio.Event()
    received = []

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "welcome", "acked_seq": 0,
                                  "capabilities": {"harness_diagnostic_v1": capability}}))
        connected.set()
        async for raw in ws:
            frame = json.loads(raw)
            received.append(frame)
            # A source that has not reached the public stream must remain retryable.
            if (frame["type"] == "harness.diagnostic" and acknowledge
                    and any(item.get("event", {}).get("type") == "usage.reported"
                            for item in received)):
                await ws.send(json.dumps({"v": 1, "type": "harness.diagnostic_ack",
                                          "schema": SCHEMA, "status": "accepted",
                                          "diagnostic_id": frame["diagnostic_id"]}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = GatewayClient(
            url=f"ws://127.0.0.1:{port}", token="test", project_id="p", runner_version="test",
            session_id=None, journal=journal, health_file=tmp_path / "health",
            inbox_dir=tmp_path / "inbox", project_root="/workspace/project", last_seq=lambda: 3,
            fork_questions=lambda _: None, poll_s=30, heartbeat_s=30,
        )
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(connected.wait(), 1)
            rows = [
                {"seq": 1, "ts": 1, "kind": "turn_started", "turn_id": "r", "payload": {}},
                {"seq": 2, "ts": 2, "kind": "sdk", "turn_id": "r", "payload": {
                    "type": "result", "session_id": "s", "total_cost_usd": 1}},
                {"seq": 3, "ts": 3, "kind": "turn_completed", "turn_id": "r", "payload": {}},
            ]
            journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
            client.drain()
            await asyncio.wait_for(task, 4.8)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    public = [frame["event"]["type"] for frame in received if frame["type"] == "event"]
    assert public == ["run.started", "usage.reported", "run.completed"]
    private = [frame for frame in received if frame["type"] == "harness.diagnostic"]
    if capability:
        assert private
        assert DiagnosticReceipts(journal).contains(private[0]["diagnostic_id"]) is acknowledge
    else:
        assert private == []


def _write_backlog(journal: Path) -> None:
    rows = [{"seq": 1, "ts": 1, "kind": "turn_started", "turn_id": "old", "payload": {}}]
    rows.extend({"seq": index + 2, "ts": index + 2, "kind": "sdk", "turn_id": "old",
                 "payload": {"type": "result", "session_id": "old", "total_cost_usd": 1}}
                for index in range(520))
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _assert_backlog_receipts(journal: Path, old_ids: set[str], fresh_ids: set[str],
                             acknowledge: bool) -> None:
    assert len(old_ids) == 520
    assert len(fresh_ids) == 1
    receipts = DiagnosticReceipts(journal)
    assert receipts.contains(next(iter(fresh_ids))) is acknowledge
    assert not any(receipts.contains(key) for key in old_ids)


def _append_fresh_result(journal: Path) -> None:
    tail = [
        {"seq": 522, "ts": 522, "kind": "turn_completed", "turn_id": "old", "payload": {}},
        {"seq": 523, "ts": 523, "kind": "turn_started", "turn_id": "fresh", "payload": {}},
        {"seq": 524, "ts": 524, "kind": "sdk", "turn_id": "fresh", "payload": {
            "type": "result", "session_id": "fresh", "total_cost_usd": 2}},
        {"seq": 525, "ts": 525, "kind": "turn_completed", "turn_id": "fresh", "payload": {}},
    ]
    with journal.open("a") as stream:
        stream.write("".join(json.dumps(row) + "\n" for row in tail))


def _backlog_client(tmp_path: Path, journal: Path, port: int) -> GatewayClient:
    return GatewayClient(
        url=f"ws://127.0.0.1:{port}", token="test", project_id="p", runner_version="test",
        session_id=None, journal=journal, health_file=tmp_path / "health",
        inbox_dir=tmp_path / "inbox", project_root="/workspace/project", last_seq=lambda: 525,
        fork_questions=lambda _: None, poll_s=0.02, heartbeat_s=30,
    )


async def _check_backlog_retry(tmp_path: Path, journal: Path, port: int,
                               retried_old: asyncio.Event) -> None:
    retry_task = asyncio.create_task(_backlog_client(tmp_path, journal, port).run())
    try:
        await asyncio.wait_for(retried_old.wait(), 2)
    finally:
        retry_task.cancel()
        await asyncio.gather(retry_task, return_exceptions=True)


@pytest.mark.parametrize("acknowledge", [True, False])
async def test_drain_reaches_fresh_result_behind_full_unacknowledged_window(
    tmp_path: Path, acknowledge: bool,
):
    journal = tmp_path / "events.jsonl"
    _write_backlog(journal)
    window_full = asyncio.Event()
    old_ids: set[str] = set()
    fresh_ids: set[str] = set()
    public_fresh = False
    connection_count = 0
    retried_old = asyncio.Event()

    async def handler(ws):
        nonlocal public_fresh, connection_count
        connection_count += 1
        reconnected = connection_count > 1
        await ws.recv()
        # An actual v1-only peer never acknowledges unbound historical diagnostics.
        await ws.send(json.dumps({"v": 1, "type": "welcome", "acked_seq": 0,
                                  "capabilities": {"harness_diagnostic_v1": True}}))
        async for raw in ws:
            frame = json.loads(raw)
            if frame["type"] == "event":
                if (frame["event"]["run_id"] == "fresh"
                        and frame["event"]["type"] == "usage.reported"):
                    public_fresh = True
            elif frame["type"] == "harness.diagnostic":
                key = frame["diagnostic_id"]
                if frame["transport_run_id"] == "old":
                    if reconnected:
                        retried_old.set()
                    old_ids.add(key)
                    if len(old_ids) == 256:
                        window_full.set()
                else:
                    fresh_ids.add(key)
                    if acknowledge and public_fresh:
                        await ws.send(json.dumps({"v": 1, "type": "harness.diagnostic_ack",
                                                  "schema": SCHEMA, "status": "accepted",
                                                  "diagnostic_id": key}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = _backlog_client(tmp_path, journal, port)
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(window_full.wait(), 3)
            _append_fresh_result(journal)
            started = time.monotonic()
            client.drain()
            await asyncio.wait_for(task, 4.8)
            assert time.monotonic() - started < 4.8
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Evicted frames are not terminal locally: a new process replays retained rows.
        await _check_backlog_retry(tmp_path, journal, port, retried_old)
    assert public_fresh
    _assert_backlog_receipts(journal, old_ids, fresh_ids, acknowledge)
