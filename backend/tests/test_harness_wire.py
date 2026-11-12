"""Real WebSocket transport with new and pinned actual old sender; no model calls."""
import asyncio
import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

from backend.runner_app import create_runner_app
from backend.runner_transport import RunnerTransport
from backend.tests.test_runner_app import eventually, live_server
from ctrunner.gateway import GatewayClient


def old_client():
    path = Path(__file__).parent / "fixtures/harness/gateway_a1d6179.py.txt"
    loader = SourceFileLoader("pinned_old_gateway", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module.GatewayClient


def write_journal(path):
    path.write_text(
        '{"seq":1,"ts":1791273601,"turn_id":"run-1","kind":"turn_started","payload":{"prompt":"SECRET"}}\n'
        '{"seq":2,"ts":1791273602,"turn_id":"run-1","kind":"sdk","payload":{"type":"result","session_id":"private","total_cost_usd":0.062745}}\n'
        '{"seq":3,"ts":1791273603,"turn_id":"run-1","kind":"turn_completed","payload":{}}\n'
    )


async def test_harness_pinned_old_sender_lost_ack_capability_toggles(tmp_path):
    transport = RunnerTransport(tmp_path / "transport.db")
    transport.message("p1", "run-1", "c1", "go", backend_run_id="backend-1")
    app = create_runner_app(transport, {"p1": "test-token-p1"})
    bridge = app.state.runner_bridge
    bridge.harness_diagnostics_enabled = True
    path = tmp_path / "events.jsonl"
    write_journal(path)
    # Leave usage at the inclusive cursor so actual old sender MUST replay that body.
    path.write_text("\n".join(path.read_text().splitlines()[:2]) + "\n")
    # Drop the first diagnostic ACK AFTER durable receipt commit.
    real_accept = transport.diagnostic
    calls = []
    def lose_first_ack(project, frame):
        ack = real_accept(project, frame)
        if ack:
            calls.append(ack)
            if len(calls) == 1:
                return None
        return ack
    transport.diagnostic = lose_first_ack
    async with live_server(app) as url:
        def client(cls):
            return cls(url=url, token="test-token-p1", project_id="p1", runner_version="test",
                session_id=None, journal=path, health_file=tmp_path / "health", inbox_dir=tmp_path / "inbox",
                project_root="/workspace/project", last_seq=lambda: 3, fork_questions=lambda _: None,
                poll_s=0.01, heartbeat_s=1, backoff=(0.01,))
        async def finish(task):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await eventually(lambda: not bridge.is_connected("p1"))
        task = asyncio.create_task(client(GatewayClient).run())
        try:
            await eventually(lambda: len(calls) >= 2)
            assert len(transport.diagnostic_snapshots("backend-1")) == 1
            before = transport.events("p1", "run-1")
            cursor = transport.hello("p1")
            await finish(task)
            # Actual old binary implementation, not a flag on the new sender.
            task = asyncio.create_task(client(old_client()).run())
            await eventually(lambda: bridge.is_connected("p1"))
            await finish(task)
            bridge.harness_diagnostics_enabled = False
            task = asyncio.create_task(client(GatewayClient).run())
            await eventually(lambda: bridge.is_connected("p1"))
            await finish(task)
            bridge.harness_diagnostics_enabled = True
            prior = len(calls)
            task = asyncio.create_task(client(GatewayClient).run())
            await eventually(lambda: len(calls) > prior)
            assert transport.events("p1", "run-1") == before
            assert transport.hello("p1") == cursor
            assert len(transport.diagnostic_snapshots("backend-1")) == 1
        finally:
            await finish(task)


async def test_new_sender_legacy_fake_gateway_sends_no_private_frame(tmp_path):
    from evals.fake_gateway import FakeGateway
    path = tmp_path / "journal"
    write_journal(path)
    async with FakeGateway() as gateway:
        client = GatewayClient(url=gateway.url, token="gw-secret-token", project_id="p1", runner_version="test",
            session_id=None, journal=path, health_file=tmp_path / "health", inbox_dir=tmp_path / "inbox",
            project_root="/workspace/project", last_seq=lambda: 3, fork_questions=lambda _: None,
            poll_s=0.01, heartbeat_s=1, backoff=(0.01,))
        task = asyncio.create_task(client.run())
        try:
            await gateway.wait_for(lambda f: f.get("event", {}).get("type") == "run.completed")
            assert not any(f.get("type") == "harness.diagnostic" for f in gateway.received)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_new_sender_pinned_production_old_receiver(tmp_path):
    def pinned(name):
        path = Path(__file__).parent / f"fixtures/harness/{name}_a1d6179.py.txt"
        loader = SourceFileLoader(f"pinned_{name}", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    old_transport = pinned("runner_transport").RunnerTransport(tmp_path / "old.db")
    old_app = pinned("runner_app").create_runner_app(old_transport, {"p1": "test-token-p1"})
    # Intercept only wire sends; actual old receiver/store dispatch and legacy ingest run unchanged.
    path = tmp_path / "journal"
    write_journal(path)
    old_transport.message("p1", "run-1", "c1", "go")
    sent = []
    class RecordingClient(GatewayClient):
        async def _send_diagnostics(self, ws, accepted):
            sent.append("private-task-started")
            await super()._send_diagnostics(ws, accepted)
    async with live_server(old_app) as url:
        client = RecordingClient(url=url, token="test-token-p1", project_id="p1", runner_version="new",
            session_id=None, journal=path, health_file=tmp_path / "health", inbox_dir=tmp_path / "inbox",
            project_root="/workspace/project", last_seq=lambda: 3, fork_questions=lambda _: None,
            poll_s=0.01, heartbeat_s=1, backoff=(0.01,))
        task = asyncio.create_task(client.run())
        try:
            await eventually(lambda: old_transport.status("p1", "run-1")["status"] == "completed")
            assert [e["type"] for e in old_transport.events("p1", "run-1")] == [
                "run.accepted", "run.started", "usage.reported", "run.completed"]
            assert old_transport.hello("p1") == 3
            assert sent == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_harness_terminal_missing_source_does_not_starve(tmp_path):
    from evals.fake_gateway import FakeGateway
    class DiagnosticGateway(FakeGateway):
        async def _handle(self, ws):
            self._ws = ws
            async for raw in ws:
                message = json.loads(raw)
                self.received.append(message)
                self._changed.set()
                if message.get("type") == "hello":
                    await ws.send(json.dumps({"v": 1, "type": "welcome", "acked_seq": 99,
                        "capabilities": {"harness_diagnostic_v1": True}}))
                if message.get("type") == "harness.diagnostic" and message["source_journal_seq"] == 5:
                    await ws.send(json.dumps({"v": 1, "type": "harness.diagnostic_ack",
                        "schema": message["schema"], "diagnostic_id": message["diagnostic_id"], "status": "accepted"}))
    path = tmp_path / "journal"
    write_journal(path)
    # Independent second lineage; first remains missing forever.
    with path.open("a") as stream:
        stream.write('{"seq":4,"ts":1791273604,"turn_id":"run-2","kind":"turn_started","payload":{"prompt":"go"}}\n')
        stream.write('{"seq":5,"ts":1791273605,"turn_id":"run-2","kind":"sdk","payload":{"type":"result","session_id":"private2","total_cost_usd":0.1}}\n')
    async with DiagnosticGateway() as gateway:
        runner = GatewayClient(url=gateway.url, token="gw-secret-token", project_id="p1", runner_version="test",
            session_id=None, journal=path, health_file=tmp_path / "health", inbox_dir=tmp_path / "inbox",
            project_root="/workspace/project", last_seq=lambda: 5, fork_questions=lambda _: None,
            poll_s=0.01, heartbeat_s=1, backoff=(0.01,))
        task = asyncio.create_task(runner.run())
        try:
            await gateway.wait_for(lambda f: f.get("type") == "harness.diagnostic" and f["source_journal_seq"] == 5)
            assert gateway.events() == []
            assert len({f["diagnostic_id"] for f in gateway.received if f.get("type") == "harness.diagnostic"}) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
