"""Shutdown flushes the final public and optional private observations together."""

import asyncio
import json
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
