"""Accepted work survives subscriber failures; diagnostic writes stay off-loop."""
import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend import main
from backend.config import Settings
from backend.models import Conversation, Run, User
from backend.ai_access import admit_run, policy_transaction
from backend.repository import finalize_run
from backend.runner_app import RunnerBridge
from backend.runner_transport import RunnerTransport
from backend.tests.test_harness_diagnostics import frame, usage
from backend.tests.test_pr20_delivery_lifecycle import database as database


class AcceptanceSocket:
    def __init__(self, app, *, failure=None, entered=None):
        self.app = app
        self.failure = failure
        self.entered = entered

    async def send_text(self, raw):
        if json.loads(raw)["type"] == "run.accepted":
            if self.entered is not None:
                self.entered.set()
                await asyncio.Event().wait()
            raise self.failure


async def owner(database):
    async with database.session() as session:
        user = User(email="acceptance@example.test", password_hash="synthetic")
        session.add(user)
        await session.commit()
        return user.id


@pytest.mark.parametrize("failure", [OSError("socket gone"), TimeoutError("send timeout")])
async def test_acceptance_delivery_failure_still_executes_and_cleans_callback(database, monkeypatch, failure):
    app = main.create_app(database, Settings(executor="runner"))
    owner_id = await owner(database)
    request_id = str(uuid4())
    calls = []

    async def executor(state, accepted, content, deliver, *, admission):
        calls.append(accepted.run_id)
        async with database.session() as session:
            await finalize_run(session, accepted.run_id, status="completed", assistant_content=None,
                               exit_code=0, session_id="synthetic-session")

    monkeypatch.setattr(main, "run_via_runner", executor)
    await main.handle_run_create(AcceptanceSocket(app, failure=failure), conversation_id=None,
        client_request_id=request_id, content="synthetic", context_mode="resume", owner_id=owner_id)
    async with database.session() as session:
        run = await session.scalar(select(Run).where(Run.client_request_id == request_id))
        assert run.status == "completed"
        assert calls == [run.id]
    assert app.state.live_run_deliveries == {}


@pytest.mark.parametrize("cancel_twice", [False, True])
async def test_acceptance_delivery_cancel_finalizes_pending_preserving_resume(database, monkeypatch, cancel_twice):
    owner_id = await owner(database)
    async with database.session() as session:
        conversation = Conversation(owner_id=owner_id, title="Synthetic", claude_session_id="previous", context_state="active")
        session.add(conversation)
        await session.commit()
        conversation_id = conversation.id
    app = main.create_app(database, Settings(executor="runner"))
    entered = asyncio.Event()
    request_id = str(uuid4())

    async def executor(*args, **kwargs):
        pytest.fail("Cancelled before execution")

    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    real_failure = main.fail_unadmitted_run

    async def blocked_failure(session, run_id):
        cleanup_entered.set()
        await cleanup_release.wait()
        return await real_failure(session, run_id)

    if cancel_twice:
        monkeypatch.setattr(main, "fail_unadmitted_run", blocked_failure)
    monkeypatch.setattr(main, "run_via_runner", executor)
    task = asyncio.create_task(main.handle_run_create(AcceptanceSocket(app, entered=entered),
        conversation_id=conversation_id, client_request_id=request_id, content="synthetic",
        context_mode="resume", owner_id=owner_id))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    if cancel_twice:
        await asyncio.wait_for(cleanup_entered.wait(), 2)
        task.cancel()
        assert not task.done()
        cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    async with database.session() as session:
        run = await session.scalar(select(Run).where(Run.client_request_id == request_id))
        conversation = await session.get(Conversation, conversation_id)
        assert run.status == "failed"
        assert run.admitted_at is None
        assert conversation.claude_session_id == "previous"
        assert conversation.context_state == "active"
    assert app.state.live_run_deliveries == {}


async def test_diagnostic_write_lock_does_not_block_loop_or_ack_before_commit(tmp_path):
    path = tmp_path / "diagnostic.sqlite3"
    transport = RunnerTransport(path)
    transport.message("p1", "run-1", "synthetic", "synthetic", backend_run_id="backend-1")
    source = usage()
    transport.ingest("p1", 2, source)
    metadata = frame(source)
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    entered = threading.Event()
    sent = []
    loop_thread = threading.get_ident()
    stop = asyncio.Event()

    def diagnostic(project, frame):
        assert threading.get_ident() != loop_thread
        entered.set()
        return transport.diagnostic(project, frame)

    class Socket:
        async def receive_json(self):
            if entered.is_set():
                await stop.wait()
            return metadata

        async def send_json(self, receipt):
            sent.append(receipt)
            stop.set()
            raise RuntimeError("test completed")

    bridge = RunnerBridge(SimpleNamespace(diagnostic=diagnostic), harness_diagnostics_enabled=True)
    task = asyncio.create_task(bridge._receive(Socket(), "p1"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        heartbeat = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(heartbeat.set_result, True)
        assert await asyncio.wait_for(heartbeat, 1)
        assert sent == []
    finally:
        blocker.rollback()
        blocker.close()
    with pytest.raises(RuntimeError, match="test completed"):
        await asyncio.wait_for(task, 2)
    assert len(sent) == 1 and sent[0]["status"] == "accepted"
    assert len(transport.diagnostic_snapshots("backend-1")) == 1


async def test_replay_delivery_cancel_does_not_terminate_another_owner(database, monkeypatch):
    owner_id = await owner(database)
    request_id = str(uuid4())
    async with database.session() as session:
        accepted = await main.accept_run(session, conversation_id=None,
            client_request_id=request_id, content="synthetic", requested_context_mode="resume",
            owner_id=owner_id)
        await policy_transaction(session, lambda: admit_run(session, accepted.run_id))
    app = main.create_app(database, Settings(executor="runner"))
    entered = asyncio.Event()
    task = asyncio.create_task(main.handle_run_create(AcceptanceSocket(app, entered=entered),
        conversation_id=accepted.conversation_id, client_request_id=request_id,
        content="synthetic", context_mode="resume", owner_id=owner_id))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "running"
    assert app.state.live_run_deliveries == {}


async def test_cancelled_ingest_wait_has_no_early_ack_and_replays_committed_receipt(tmp_path):
    transport = RunnerTransport(tmp_path / "cancelled.sqlite3")
    transport.message("p1", "run-1", "synthetic", "synthetic", backend_run_id="backend-1")
    source = usage()
    transport.ingest("p1", 2, source)
    metadata = frame(source)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    sent = []
    observed = []

    def diagnostic(project, payload):
        entered.set()
        assert release.wait(2)
        try:
            receipt = transport.diagnostic(project, payload)
            observed.append(receipt)
            return receipt
        finally:
            finished.set()

    class Socket:
        async def receive_json(self):
            return metadata

        async def send_json(self, receipt):
            sent.append(receipt)

    bridge = RunnerBridge(SimpleNamespace(diagnostic=diagnostic), harness_diagnostics_enabled=True)
    task = asyncio.create_task(bridge._receive(Socket(), "p1"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sent == []
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert transport.diagnostic("p1", metadata) == observed[0]
    assert observed[0]["status"] == "accepted"
    assert len(transport.diagnostic_snapshots("backend-1")) == 1


async def test_unexpected_acceptance_publication_error_still_cleans_owned_pending(database):
    owner_id = await owner(database)
    app = main.create_app(database, Settings(executor="runner"))
    request_id = str(uuid4())
    with pytest.raises(ValueError, match="invalid serialization"):
        await main.handle_run_create(AcceptanceSocket(app, failure=ValueError("invalid serialization")),
            conversation_id=None, client_request_id=request_id, content="synthetic",
            context_mode="resume", owner_id=owner_id)
    async with database.session() as session:
        run = await session.scalar(select(Run).where(Run.client_request_id == request_id))
        assert run.status == "failed"
        assert run.admitted_at is None
    assert app.state.live_run_deliveries == {}


async def test_diagnostic_database_failure_never_acknowledges_success():
    sent = []

    def diagnostic(project, payload):
        raise sqlite3.OperationalError("synthetic lock deadline")

    class Socket:
        async def receive_json(self):
            return {"v": 1, "type": "harness.diagnostic"}

        async def send_json(self, receipt):
            sent.append(receipt)

    bridge = RunnerBridge(SimpleNamespace(diagnostic=diagnostic), harness_diagnostics_enabled=True)
    with pytest.raises(sqlite3.OperationalError, match="synthetic lock deadline"):
        await bridge._receive(Socket(), "p1")
    assert sent == []
