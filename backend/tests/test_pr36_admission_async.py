"""Cancellation ownership and nonblocking optional diagnostic I/O."""
import asyncio
import threading
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.tests.auth_helpers import sign_in
from backend.tests.test_runner_executor import (
    FakeDocker, FakeRunner, make_runner_app, receive_until_terminal, run_command,
)

from backend import main
from backend.models import Run
from backend.tests.test_reset_admission import accept_reset, reset_db as reset_db


@pytest.mark.parametrize("phase", ["before", "after", "commit"])
async def test_cancellation_during_admission_persists_outcome(reset_db, monkeypatch, phase):
    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    entered, release = asyncio.Event(), asyncio.Event()
    real_transaction = main.policy_transaction

    async def blocked_transaction(session, operation):
        if phase == "commit":
            real_commit = session.commit

            async def blocked_commit():
                entered.set()
                await release.wait()
                await real_commit()

            monkeypatch.setattr(session, "commit", blocked_commit)
        if phase == "before":
            entered.set()
            await release.wait()
        result = await real_transaction(session, operation)
        if phase == "after":
            entered.set()
            await release.wait()
        return result

    async def execute(*args, **kwargs):
        pytest.fail("Cancelled owner must not execute the model")

    async def deliver(event):
        pass

    monkeypatch.setattr(main, "policy_transaction", blocked_transaction)
    monkeypatch.setattr(main, "run_via_runner", execute)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    task = asyncio.create_task(main.execute_accepted_run(state, accepted, "synthetic", deliver))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "outcome_unknown"


async def test_cancelled_duplicate_does_not_finalize_other_owner(reset_db, monkeypatch):
    from backend.ai_access import admit_run, policy_transaction

    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    async with database.session() as session:
        await policy_transaction(session, lambda: admit_run(session, accepted.run_id))
    entered, release = asyncio.Event(), asyncio.Event()
    real_transaction = main.policy_transaction

    async def blocked_transaction(session, operation):
        entered.set()
        await release.wait()
        return await real_transaction(session, operation)

    monkeypatch.setattr(main, "policy_transaction", blocked_transaction)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    task = asyncio.create_task(main.execute_accepted_run(state, accepted, "synthetic", None))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "running"


async def test_detail_transport_read_does_not_block_event_loop(monkeypatch, tmp_path):
    from backend.api import admin

    entered, release = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    path = tmp_path / "locked.db"
    blocker = sqlite3.connect(path)
    blocker.execute("CREATE TABLE diagnostics (value INTEGER)")
    blocker.commit()
    blocker.execute("BEGIN EXCLUSIVE")

    def blocking_read(run_id):
        assert threading.get_ident() != loop_thread
        entered.set()
        with sqlite3.connect(path, timeout=2) as connection:
            connection.execute("SELECT * FROM diagnostics").fetchall()
        release.set()
        return []

    async def catch_up(*args):
        pass

    async def detail(*args):
        return {"run": {"id": "run"}}

    monkeypatch.setattr("backend.harness_estimate.catch_up", catch_up)
    monkeypatch.setattr(admin.admin_queries, "run_detail", detail)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        runner_transport=SimpleNamespace(diagnostic_snapshots=blocking_read), database=None)))
    task = asyncio.create_task(admin.operational_detail("run", request, None, None))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        heartbeat = loop.create_future()
        loop.call_soon(heartbeat.set_result, True)
        assert await asyncio.wait_for(heartbeat, 1)
        assert not release.is_set()
    finally:
        blocker.rollback()
        blocker.close()
    assert (await task).status_code == 200
    assert release.is_set()


def test_terminal_diagnostic_read_runs_outside_application_thread(tmp_path):
    app = make_runner_app(tmp_path, FakeDocker())
    calls = []
    with TestClient(app) as client:
        sign_in(client)
        application_thread = client.portal.call(threading.get_ident)

        def snapshots(run_id):
            calls.append(threading.get_ident())
            assert calls[-1] != application_thread
            return []

        app.state.runner_transport.diagnostic_snapshots = snapshots
        runner = FakeRunner(app.state.runner_transport, app.state.runner_manager._client,
                            lambda command, container: [("run.started", {}), ("run.completed", {})])
        runner.start()
        try:
            with client.websocket_connect("/ws") as socket:
                socket.send_json(run_command("synthetic"))
                events = receive_until_terminal(socket)
            assert events[-1]["type"] == "run.completed"
            assert calls
        finally:
            runner.halt.set()
            runner.join(timeout=3)


@pytest.mark.parametrize("already_admitted", [False, True])
async def test_deadline_during_post_commit_session_cleanup(reset_db, monkeypatch, already_admitted):
    from contextlib import asynccontextmanager
    from backend.ai_access import admit_run, policy_transaction

    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    if already_admitted:
        async with database.session() as session:
            await policy_transaction(session, lambda: admit_run(session, accepted.run_id))
    real_session = database.session
    first = True

    @asynccontextmanager
    async def delayed_cleanup():
        nonlocal first
        delay = first
        first = False
        async with real_session() as session:
            yield session
            if delay:
                await asyncio.Event().wait()

    original_timeout = asyncio.timeout
    monkeypatch.setattr(database, "session", delayed_cleanup)
    monkeypatch.setattr(asyncio, "timeout", lambda delay: original_timeout(0.05 if delay == 30 else delay))

    async def executor(*args, **kwargs):
        pytest.fail("No executor after expired admission handoff")

    monkeypatch.setattr(main, "run_via_runner", executor)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    if already_admitted:
        await main.execute_accepted_run(state, accepted, "synthetic", None)
    else:
        with pytest.raises(TimeoutError):
            await main.execute_accepted_run(state, accepted, "synthetic", None)
    async with real_session() as session:
        run = await session.get(Run, accepted.run_id)
        assert run.status == ("running" if already_admitted else "outcome_unknown")
