"""COMMIT outcome uncertainty: apply durably, then lose the application acknowledgement."""
import asyncio
import errno
import socket
import sqlite3

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeoutError

from backend import main
from backend.ai_access import (
    AiAccessError, admit_run, fail_admission_handoff, policy_transaction,
)
from backend.public_events import message_created_event, run_started_event
from backend.models import Conversation, Event, Run
from backend.repository import RunAcceptanceError, accept_run, append_event, finalize_run
from backend.tests.test_reset_admission import reset_db as reset_db


@pytest.mark.parametrize("applied", [True, False])
@pytest.mark.parametrize("context_mode", ["resume", "reset"])
@pytest.mark.parametrize("rollback_broken", [False, True])
async def test_commit_ack_loss_is_not_rollback(
    reset_db, monkeypatch, applied, context_mode, rollback_broken,
):
    database, conversation_id = reset_db
    key = str(uuid4())
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=key, content="continue", requested_context_mode=context_mode,
            owner_id="reset-owner")
    real_transaction = main.policy_transaction
    observations = []

    async def uncertain_commit(session, operation):
        real_commit = session.commit
        real_rollback = session.rollback

        async def lose_ack():
            if applied:
                await real_commit()
            else:
                await real_rollback()
            # A different session sees the committed database BEFORE the application
            # commit coroutine returns. This is not an exception during session close.
            async with database.session() as fresh:
                run = await fresh.get(Run, accepted.run_id)
                started = list(await fresh.scalars(select(Event).where(
                    Event.run_id == run.id, Event.type == "run.started")))
                observations.append((run.status, run.admitted_at is not None, len(started)))
            raise DBAPIError("COMMIT", None, ConnectionError("acknowledgement lost"),
                             connection_invalidated=True)

        monkeypatch.setattr(session, "commit", lose_ack)
        if rollback_broken:
            real_rollback = session.rollback

            async def broken_rollback():
                await real_rollback()
                raise DBAPIError("ROLLBACK", None, ConnectionError("connection lost"))

            monkeypatch.setattr(session, "rollback", broken_rollback)
        return await real_transaction(session, operation)

    async def forbidden(*args, **kwargs):
        pytest.fail("An uncertain admission must never invoke an executor")

    async def deliver(event):
        pass

    monkeypatch.setattr(main, "policy_transaction", uncertain_commit)
    monkeypatch.setattr(main, "run_via_runner", forbidden)
    await main.execute_accepted_run(SimpleNamespace(database=database,
        settings=SimpleNamespace(executor="runner")), accepted, "continue", deliver)
    assert observations == [("running", True, 1) if applied else ("pending", False, 0)]
    async with database.session() as session:
        run = await session.get(Run, accepted.run_id)
        conversation = await session.get(Conversation, conversation_id)
        assert run.status == "failed"
        assert run.error_code == "ai_policy_unavailable"
        reset_applied = applied and context_mode == "reset"
        assert (conversation.claude_session_id, conversation.context_state) == (
            (None, "unavailable") if reset_applied else ("previous-session", "active"))
        events = list(await session.scalars(select(Event).where(Event.run_id == run.id).order_by(Event.seq)))
        expected = ["run.accepted"]
        if reset_applied:
            expected.append("context.reset")
        if applied:
            expected.append("run.started")
        assert [event.type for event in events] == expected + ["run.failed"]
        replay = await accept_run(session, conversation_id=conversation_id,
            client_request_id=key, content="continue", requested_context_mode=context_mode,
            owner_id="reset-owner")
        assert replay.idempotent_replay
        if reset_applied:
            with pytest.raises(RunAcceptanceError) as refused:
                await accept_run(session, conversation_id=conversation_id,
                    client_request_id=str(uuid4()), content="next", owner_id="reset-owner")
            assert refused.value.code == "conversation_context_unavailable"
            await session.rollback()
        else:
            following = await accept_run(session, conversation_id=conversation_id,
                client_request_id=str(uuid4()), content="next", owner_id="reset-owner")
            assert following.resume_session_id == "previous-session"


@pytest.mark.parametrize("activity", ["other_owner", "message", "started_again", "terminal"])
async def test_recovery_never_finalizes_unowned_or_advanced_run(reset_db, activity):
    database, conversation_id = reset_db
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    attempt_id = str(uuid4())
    async with database.session() as session:
        await policy_transaction(session, lambda: admit_run(session, accepted.run_id, attempt_id=attempt_id))
    async with database.session() as session:
        if activity == "message":
            await append_event(session, accepted.run_id, message_created_event("concurrent"))
        elif activity == "started_again":
            await append_event(session, accepted.run_id, run_started_event())
        elif activity == "terminal":
            await finalize_run(session, accepted.run_id, assistant_content=None,
                status="completed", exit_code=0, session_id="previous-session")
    async with database.session() as session:
        before = list(await session.scalars(select(Event.id).where(Event.run_id == accepted.run_id)))
        result = await fail_admission_handoff(session, accepted.run_id,
            str(uuid4()) if activity == "other_owner" else attempt_id, allow_pending=True)
        await session.commit()
        assert result is None
    async with database.session() as session:
        run = await session.get(Run, accepted.run_id)
        assert run.status == ("completed" if activity == "terminal" else "running")
        assert list(await session.scalars(select(Event.id).where(Event.run_id == run.id))) == before


async def test_retryable_lost_commit_reconciles_original_attempt(reset_db, monkeypatch):
    database, conversation_id = reset_db
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    real_transaction = main.policy_transaction
    commits = []

    class RetryableDisconnect(ConnectionError):
        sqlstate = "57014"

    async def uncertain_transaction(session, operation):
        real_commit = session.commit

        async def lose_ack():
            await real_commit()
            commits.append(True)
            raise DBAPIError("COMMIT", None, RetryableDisconnect("ack lost"))

        monkeypatch.setattr(session, "commit", lose_ack)
        return await real_transaction(session, operation)

    async def forbidden(*args, **kwargs):
        pytest.fail("No executor after lost acknowledgement")

    async def deliver(event):
        pass

    monkeypatch.setattr(main, "policy_transaction", uncertain_transaction)
    monkeypatch.setattr(main, "run_via_runner", forbidden)
    await main.execute_accepted_run(SimpleNamespace(database=database,
        settings=SimpleNamespace(executor="runner")), accepted, "continue", deliver)
    assert commits == [True]
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "failed"


async def test_repeated_cancellation_waits_for_uncertain_commit_recovery(reset_db, monkeypatch):
    database, conversation_id = reset_db
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    committed, release_commit = asyncio.Event(), asyncio.Event()
    recovering, release_recovery = asyncio.Event(), asyncio.Event()
    real_transaction = main.policy_transaction
    real_recover = main.fail_admission_handoff

    async def uncertain_transaction(session, operation):
        real_commit = session.commit

        async def lose_ack():
            await real_commit()
            committed.set()
            await release_commit.wait()
            raise DBAPIError("COMMIT", None, ConnectionError("ack lost"))

        monkeypatch.setattr(session, "commit", lose_ack)
        return await real_transaction(session, operation)

    async def delayed_recovery(*args, **kwargs):
        recovering.set()
        await release_recovery.wait()
        return await real_recover(*args, **kwargs)

    async def forbidden(*args, **kwargs):
        pytest.fail("Cancelled uncertain admission must never execute or publish")

    monkeypatch.setattr(main, "policy_transaction", uncertain_transaction)
    monkeypatch.setattr(main, "fail_admission_handoff", delayed_recovery)
    monkeypatch.setattr(main, "run_via_runner", forbidden)
    task = asyncio.create_task(main.execute_accepted_run(SimpleNamespace(database=database,
        settings=SimpleNamespace(executor="runner")), accepted, "continue", forbidden))
    await asyncio.wait_for(committed.wait(), 2)
    task.cancel()
    release_commit.set()
    await asyncio.wait_for(recovering.wait(), 2)
    task.cancel()
    release_recovery.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "failed"


async def test_stale_same_attempt_is_not_a_new_execution_admission(reset_db):
    database, conversation_id = reset_db
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    attempt_id = str(uuid4())
    async with database.session() as session:
        await policy_transaction(session, lambda: admit_run(session, accepted.run_id, attempt_id=attempt_id))
    async with database.session() as session:
        with pytest.raises(AiAccessError):
            await policy_transaction(session, lambda: admit_run(session, accepted.run_id, attempt_id=attempt_id))
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "running"


async def test_rollback_racing_with_rival_admission_preserves_rival(reset_db, monkeypatch):
    database, conversation_id = reset_db
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    real_transaction = main.policy_transaction
    rival_id = str(uuid4())

    async def rolled_back_transaction(session, operation):
        async def lose_ack():
            await session.rollback()
            async with database.session() as rival:
                await policy_transaction(rival, lambda: admit_run(rival, accepted.run_id, attempt_id=rival_id))
            raise DBAPIError("COMMIT", None, ConnectionError("original rolled back"))

        monkeypatch.setattr(session, "commit", lose_ack)
        return await real_transaction(session, operation)

    async def forbidden(*args, **kwargs):
        pytest.fail("Losing attempt must not execute or finalize the rival")

    monkeypatch.setattr(main, "policy_transaction", rolled_back_transaction)
    monkeypatch.setattr(main, "run_via_runner", forbidden)
    await main.execute_accepted_run(SimpleNamespace(database=database,
        settings=SimpleNamespace(executor="runner")), accepted, "continue", forbidden)
    async with database.session() as session:
        run = await session.get(Run, accepted.run_id)
        assert (run.status, run.admission_attempt_id) == ("running", rival_id)
        assert list(await session.scalars(select(Event.type).where(Event.run_id == run.id).order_by(Event.seq))) == [
            "run.accepted", "run.started"]


async def test_cleanup_dbapi_error_does_not_mask_unknown_commit_recovery(reset_db, monkeypatch):
    database, conversation_id = reset_db
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    real_transaction = main.policy_transaction
    real_session = database.session
    first = True

    async def uncertain_transaction(session, operation):
        real_commit = session.commit

        async def lose_ack():
            await real_commit()
            raise DBAPIError("COMMIT", None, ConnectionError("ack lost"))

        monkeypatch.setattr(session, "commit", lose_ack)
        return await real_transaction(session, operation)

    @asynccontextmanager
    async def broken_cleanup():
        nonlocal first
        fail = first
        first = False
        try:
            async with real_session() as session:
                yield session
        finally:
            if fail:
                raise DBAPIError("CLOSE", None, ConnectionError("cleanup lost"))

    async def deliver(event):
        pass

    monkeypatch.setattr(database, "session", broken_cleanup)
    monkeypatch.setattr(main, "policy_transaction", uncertain_transaction)
    await main.execute_accepted_run(SimpleNamespace(database=database,
        settings=SimpleNamespace(executor="runner")), accepted, "continue", deliver)
    async with real_session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "failed"


@pytest.mark.parametrize("failure", ["connection", "dbapi", "deadline", "pool"])
async def test_restored_database_releases_admission_without_restart(reset_db, monkeypatch, failure):
    database, conversation_id = reset_db
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    real_transaction = main.policy_transaction
    real_recover = main.fail_admission_handoff
    recovery_attempts = []

    async def uncertain_transaction(session, operation):
        real_commit = session.commit

        async def lose_ack():
            await real_commit()
            raise DBAPIError("COMMIT", None, ConnectionError("ack lost"))

        monkeypatch.setattr(session, "commit", lose_ack)
        return await real_transaction(session, operation)

    async def temporarily_unavailable(*args, **kwargs):
        recovery_attempts.append(True)
        if len(recovery_attempts) == 1:
            if failure == "connection":
                raise ConnectionRefusedError("database unavailable")
            if failure == "deadline":
                raise TimeoutError("recovery window expired")
            if failure == "pool":
                raise PoolTimeoutError("pool temporarily exhausted")
            raise DBAPIError("BEGIN", None, ConnectionError("recovery database unavailable"))
        return await real_recover(*args, **kwargs)

    async def forbidden(*args, **kwargs):
        pytest.fail("Uncertain admission must never execute")

    async def deliver(event):
        pass

    monkeypatch.setattr(main, "policy_transaction", uncertain_transaction)
    monkeypatch.setattr(main, "fail_admission_handoff", temporarily_unavailable)
    monkeypatch.setattr(main, "run_via_runner", forbidden)
    await asyncio.wait_for(main.execute_accepted_run(SimpleNamespace(database=database,
        settings=SimpleNamespace(executor="runner")), accepted, "continue", deliver), 2)
    assert len(recovery_attempts) == 2
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "failed"
        following = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="next", owner_id="reset-owner")
        assert following.resume_session_id == "previous-session"


async def test_recovery_does_not_retry_programming_database_failure(reset_db, monkeypatch):
    database, conversation_id = reset_db
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    attempts = []

    async def admission_unavailable(*args, **kwargs):
        raise ConnectionRefusedError("connection refused")

    async def programming_error(*args, **kwargs):
        attempts.append(True)
        raise DBAPIError("SELECT", None, ValueError("invalid query"))

    monkeypatch.setattr(main, "policy_transaction", admission_unavailable)
    monkeypatch.setattr(main, "fail_admission_handoff", programming_error)
    with pytest.raises(DBAPIError, match="invalid query"):
        await main.execute_accepted_run(SimpleNamespace(database=database,
            settings=SimpleNamespace(executor="runner")), accepted, "continue", None)
    assert attempts == [True]


@pytest.mark.parametrize("boundary", ["admission", "recovery"])
@pytest.mark.parametrize("network_errno", [errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.EACCES,
                                          socket.EAI_AGAIN, socket.EAI_NONAME])
async def test_fresh_connection_network_errors_preserve_owned_recovery(
    reset_db, monkeypatch, boundary, network_errno,
):
    database, conversation_id = reset_db
    if database.engine.dialect.name != "postgresql":
        pytest.skip("The asyncpg connection creator requires PostgreSQL")
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    real_transaction = main.policy_transaction
    connect_attempts = []
    real_session = database.session
    failing_connection_number = 1 if boundary == "admission" else 2

    async def connect():
        connect_attempts.append(True)
        if len(connect_attempts) == failing_connection_number:
            if network_errno in {socket.EAI_AGAIN, socket.EAI_NONAME}:
                raise socket.gaierror(network_errno, "simulated resolver failure")
            raise OSError(network_errno, "simulated connect failure")
        return await asyncpg.connect(make_url(database.url).set(
            drivername="postgresql").render_as_string(hide_password=False))

    engine = create_async_engine(database.url, async_creator=connect, poolclass=NullPool)
    monkeypatch.setattr(database, "session_factory", async_sessionmaker(engine, expire_on_commit=False))

    async def uncertain_transaction(session, operation):
        real_commit = session.commit

        async def lose_ack():
            await real_commit()
            raise DBAPIError("COMMIT", None, ConnectionError("ack lost"))

        if boundary == "recovery":
            monkeypatch.setattr(session, "commit", lose_ack)
        return await real_transaction(session, operation)

    async def forbidden(*args, **kwargs):
        pytest.fail("Connect failure must not authorize the executor")

    async def deliver(event):
        pass

    monkeypatch.setattr(main, "policy_transaction", uncertain_transaction)
    monkeypatch.setattr(main, "run_via_runner", forbidden)
    try:
        state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
        if network_errno in {errno.EACCES, socket.EAI_NONAME}:
            with pytest.raises(OSError) as caught:
                await main.execute_accepted_run(state, accepted, "continue", deliver)
            assert caught.value.errno == network_errno
            assert len(connect_attempts) == failing_connection_number
        else:
            await asyncio.wait_for(main.execute_accepted_run(state, accepted, "continue", deliver), 3)
            async with real_session() as session:
                assert (await session.get(Run, accepted.run_id)).status == "failed"
                following = await accept_run(session, conversation_id=conversation_id,
                    client_request_id=str(uuid4()), content="next", owner_id="reset-owner")
                assert following.resume_session_id == "previous-session"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("sqlite_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_READONLY])
async def test_sqlite_recovery_retries_only_lock_contention(reset_db, monkeypatch, sqlite_code):
    database, conversation_id = reset_db
    if database.engine.dialect.name != "sqlite":
        pytest.skip("SQLite contention requires the SQLite driver")
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    real_transaction = main.policy_transaction
    real_recover = main.fail_admission_handoff
    attempts = []

    async def uncertain_transaction(session, operation):
        real_commit = session.commit

        async def lose_ack():
            await real_commit()
            raise DBAPIError("COMMIT", None, ConnectionError("ack lost"))

        monkeypatch.setattr(session, "commit", lose_ack)
        return await real_transaction(session, operation)

    async def contended_recovery(session, *args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            if sqlite_code == sqlite3.SQLITE_BUSY:
                blocker = sqlite3.connect(database.engine.url.database)
                blocker.execute("BEGIN IMMEDIATE")
                await session.connection()
                await session.execute(text("PRAGMA busy_timeout=0"))
                try:
                    return await real_recover(session, *args, **kwargs)
                finally:
                    blocker.rollback()
                    blocker.close()
            error = sqlite3.OperationalError("table locked" if sqlite_code == sqlite3.SQLITE_LOCKED else "readonly")
            error.sqlite_errorcode = sqlite_code
            raise DBAPIError("BEGIN IMMEDIATE", None, error)
        return await real_recover(session, *args, **kwargs)

    async def forbidden(*args, **kwargs):
        pytest.fail("No executor after uncertain admission")

    async def deliver(event):
        pass

    monkeypatch.setattr(main, "policy_transaction", uncertain_transaction)
    monkeypatch.setattr(main, "fail_admission_handoff", contended_recovery)
    monkeypatch.setattr(main, "run_via_runner", forbidden)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    if sqlite_code == sqlite3.SQLITE_READONLY:
        with pytest.raises(DBAPIError):
            await main.execute_accepted_run(state, accepted, "continue", deliver)
        assert attempts == [True]
    else:
        await asyncio.wait_for(main.execute_accepted_run(state, accepted, "continue", deliver), 3)
        async with database.session() as session:
            assert (await session.get(Run, accepted.run_id)).status == "failed"
            assert list(await session.scalars(select(Event.type).where(
                Event.run_id == accepted.run_id, Event.type == "run.failed"))) == ["run.failed"]
        assert attempts == [True, True]


@pytest.mark.parametrize("admission_applied", [True, False])
@pytest.mark.parametrize("terminal_applied", [True, False])
@pytest.mark.parametrize("rival_terminal", [False, True])
async def test_recovery_commit_ack_loss_delivers_one_persisted_terminal(
    reset_db, monkeypatch, admission_applied, terminal_applied, rival_terminal,
):
    database, conversation_id = reset_db
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    real_transaction = main.policy_transaction
    real_recover = main.fail_admission_handoff
    injected = False
    persisted_terminal_ids = []
    delivered = []

    async def uncertain_transaction(session, operation):
        real_commit = session.commit

        async def lose_ack():
            if admission_applied:
                await real_commit()
            else:
                await session.rollback()
            raise DBAPIError("COMMIT", None, ConnectionError("admission ack lost"))

        monkeypatch.setattr(session, "commit", lose_ack)
        return await real_transaction(session, operation)

    async def uncertain_finalization(session, *args, **kwargs):
        nonlocal injected
        result = await real_recover(session, *args, **kwargs)
        if not injected:
            injected = True
            real_commit = session.commit

            async def lose_terminal_ack():
                if terminal_applied:
                    await real_commit()
                else:
                    await session.rollback()
                    if rival_terminal:
                        async with database.session() as rival:
                            await finalize_run(rival, accepted.run_id, assistant_content=None,
                                status="completed", exit_code=0, session_id="previous-session")
                async with database.session() as fresh:
                    terminal = await fresh.scalar(select(Event).where(
                        Event.run_id == accepted.run_id,
                        Event.type.in_({"run.failed", "run.completed"})))
                    if terminal is not None:
                        persisted_terminal_ids.append(terminal.id)
                raise DBAPIError("COMMIT", None, ConnectionError("terminal ack lost"))

            monkeypatch.setattr(session, "commit", lose_terminal_ack)
        return result

    async def deliver(event):
        delivered.append(event.id)

    async def forbidden(*args, **kwargs):
        pytest.fail("Unknown admission must not execute")

    monkeypatch.setattr(main, "policy_transaction", uncertain_transaction)
    monkeypatch.setattr(main, "fail_admission_handoff", uncertain_finalization)
    monkeypatch.setattr(main, "run_via_runner", forbidden)
    await asyncio.wait_for(main.execute_accepted_run(SimpleNamespace(database=database,
        settings=SimpleNamespace(executor="runner")), accepted, "continue", deliver), 3)
    async with database.session() as session:
        stored = list(await session.scalars(select(Event.id).where(
            Event.run_id == accepted.run_id, Event.type.in_({"run.failed", "run.completed"}))))
        if persisted_terminal_ids:
            assert stored == persisted_terminal_ids
        assert len(stored) == 1
        assert delivered == stored
        run = await session.get(Run, accepted.run_id)
        assert run.status == ("completed" if rival_terminal and not terminal_applied else "failed")


@pytest.mark.parametrize("phase", ["window", "backoff"])
async def test_recovery_cancellation_is_bounded_without_detached_retry(reset_db, monkeypatch, phase):
    database, conversation_id = reset_db
    async with database.session() as session:
        accepted = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="continue", owner_id="reset-owner")
    real_transaction = main.policy_transaction
    original_timeout = asyncio.timeout
    recovery_timeout = None
    recovery_entered = asyncio.Event()
    cancelled_window = asyncio.Event()
    attempts = []

    def tracked_timeout(delay):
        nonlocal recovery_timeout
        timeout = original_timeout(delay)
        if delay == 10:
            recovery_timeout = timeout
        return timeout

    async def uncertain_transaction(session, operation):
        real_commit = session.commit

        async def lose_ack():
            await real_commit()
            raise DBAPIError("COMMIT", None, ConnectionError("ack lost"))

        monkeypatch.setattr(session, "commit", lose_ack)
        return await real_transaction(session, operation)

    async def unavailable_recovery(*args, **kwargs):
        attempts.append(True)
        recovery_entered.set()
        if phase == "backoff":
            raise ConnectionRefusedError("database still unavailable")
        assert recovery_timeout is not None
        await cancelled_window.wait()
        recovery_timeout.reschedule(asyncio.get_running_loop().time())
        await asyncio.Event().wait()

    async def forbidden(*args, **kwargs):
        pytest.fail("Recovery deadline must not execute or claim success")

    monkeypatch.setattr(asyncio, "timeout", tracked_timeout)
    monkeypatch.setattr(main, "policy_transaction", uncertain_transaction)
    monkeypatch.setattr(main, "fail_admission_handoff", unavailable_recovery)
    monkeypatch.setattr(main, "run_via_runner", forbidden)
    task = asyncio.create_task(main.execute_accepted_run(SimpleNamespace(database=database,
        settings=SimpleNamespace(executor="runner")), accepted, "continue", forbidden))
    await asyncio.wait_for(recovery_entered.wait(), 2)
    task.cancel()
    cancelled_window.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert attempts == [True]
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "running"
