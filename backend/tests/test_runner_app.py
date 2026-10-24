import asyncio
import json
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.runner_app import RunnerBridge, create_runner_app
from backend.runner_transport import RunnerTransport


def test_auth_project_binding_and_no_public_command_endpoint(tmp_path):
    transport = RunnerTransport(tmp_path / "transport.db")
    app = create_runner_app(transport, {"p1": "test-token-p1", "p2": "test-token-p2"})
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/runner"):
                pass
        with client.websocket_connect("/v1/runner", headers={"Authorization": "Bearer test-token-p1"}) as ws:
            ws.send_json({"v": 1, "type": "hello", "project_id": "p2",
                          "runner_version": "0.1", "last_seq": 0, "session_id": None})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert client.post("/v1/runner/commands", json={}).status_code == 404


def test_duplicate_project_connection_and_wrong_wire_version(tmp_path):
    app = create_runner_app(RunnerTransport(tmp_path / "transport.db"), {"p1": "test-token-p1"})
    hello = {"v": 1, "type": "hello", "project_id": "p1",
             "runner_version": "0.1", "last_seq": 0, "session_id": None}
    headers = {"Authorization": "Bearer test-token-p1"}
    with TestClient(app) as client:
        with client.websocket_connect("/v1/runner", headers=headers) as first:
            first.send_json(hello)
            assert first.receive_json() == {"v": 1, "type": "welcome", "acked_seq": 0}
            with client.websocket_connect("/v1/runner", headers=headers) as second:
                second.send_json(hello)
                with pytest.raises(WebSocketDisconnect):
                    second.receive_json()
            first.send_json({"v": 2, "type": "heartbeat"})
            with pytest.raises(WebSocketDisconnect):
                first.receive_json()


@pytest.mark.parametrize("frame", [
    {"v": 1, "type": "event", "journal_seq": 1,
     "event": {"event_id": "00000000-0000-0000-0000-000000000001", "project_id": "p1",
               "run_id": "run-1", "occurred_at": "2026-10-16T07:00:00Z", "type": [], "payload": {}}},
    {"v": 1, "type": "heartbeat", "phase": [], "health": "ok", "last_progress_at": 1},
    {"v": 1, "type": "heartbeat", "phase": "idle", "health": {}, "last_progress_at": 1},
    {"v": 1, "type": "command_ack", "id": "run-1", "status": []},
])
def test_malformed_nested_frame_closes_cleanly(tmp_path, frame):
    transport = RunnerTransport(tmp_path / "transport.db")
    transport.message("p1", "run-1", "c1", "go")
    app = create_runner_app(transport, {"p1": "test-token-p1"})
    with TestClient(app) as client:
        with client.websocket_connect("/v1/runner", headers={"Authorization": "Bearer test-token-p1"}) as ws:
            ws.send_json({"v": 1, "type": "hello", "project_id": "p1",
                          "runner_version": "0.1", "last_seq": 0, "session_id": None})
            assert ws.receive_json()["type"] == "welcome"
            ws.send_json(frame)
            with pytest.raises(WebSocketDisconnect):
                while True:  # a queued command may precede the close
                    ws.receive_json()


@asynccontextmanager
async def live_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = f"ws://127.0.0.1:{sock.getsockname()[1]}/v1/runner"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="websockets-sansio", http="h11"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        yield address
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_real_gateway_client_question_answer_and_reconnect(tmp_path):
    # Actual pinned runner client, translator, event log and inbox; NO Claude/SDK call.
    from ctrunner.eventlog import EventLog
    from ctrunner.gateway import GatewayClient
    from ctrunner.inbox import Inbox
    from ctrunner.protocol import EventKind
    from ctrunner.redact import Redactor

    transport = RunnerTransport(tmp_path / "transport.db")
    app = create_runner_app(transport, {"p1": "test-token-p1"})
    journal = tmp_path / "events.jsonl"
    health = tmp_path / "health.json"
    inbox = tmp_path / "inbox"
    health.write_text(json.dumps({"phase": "awaiting_answer", "health": "ok",
                                 "last_progress_at": 1.0}), encoding="utf-8")
    log = EventLog.open(journal, Redactor.of([]), lambda: 1791273600.0)
    forks = {"q1": ("Format?",)}
    async with live_server(app) as url:
        runner = GatewayClient(
            url=url, token="test-token-p1", project_id="p1", runner_version="0.1.0",
            session_id=None, journal=journal, health_file=health, inbox_dir=inbox,
            project_root="/workspace/project", last_seq=lambda: log.last_seq,
            fork_questions=forks.get, poll_s=0.02, heartbeat_s=0.05, backoff=(0.02,))
        task = asyncio.create_task(runner.run())
        try:
            transport.message("p1", "run-1", "c1", "review", roles=["reviewer"],
                              stage="analysis", input_files=["inputs/a.md"])
            await eventually(lambda: transport.ack_receipt("p1", "run-1") is not None)
            message = next(d.command for d in Inbox(inbox).peek() if d.command.id == "run-1")
            assert message.roles == ("reviewer",) and message.input_files == ("inputs/a.md",)
            log.append(EventKind.TURN_STARTED, "run-1", {"prompt": "review"})
            log.append(EventKind.FORK_QUESTION, "run-1", {"fork_id": "q1", "questions": [
                {"question": "Format?", "options": [{"label": "md"}]}]})
            await eventually(lambda: transport.status("p1", "run-1")["status"] == "waiting_for_input")
            transport.answer("p1", "run-1", "a1", "q1", {"Format?": "md"})
            await eventually(lambda: transport.ack_receipt("p1", "a1") is not None)
            assert transport.ack_receipt("p1", "a1")["status"] == "accepted"
            log.append(EventKind.FORK_ANSWERED, "run-1", {"fork_id": "q1", "answers": {"Format?": "md"}})
            await eventually(lambda: transport.status("p1", "run-1")["status"] == "running")
            count = len(transport.events("p1", "run-1"))
            # Reconnect actual client: inclusive boundary replays last row and dedups it.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await eventually(lambda: not app.state.runner_bridge._connections)
            task = asyncio.create_task(runner.run())
            await eventually(lambda: bool(app.state.runner_bridge._connections))
            log.append(EventKind.TURN_COMPLETED, "run-1", {})
            await eventually(lambda: transport.status("p1", "run-1")["status"] == "completed")
            events = transport.events("p1", "run-1")
            assert len(events) == count + 1
            assert {e["run_id"] for e in events} == {"run-1"}
            assert events[-1]["type"] == "run.completed"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            log.close()
