"""Pre-executor failures must not invalidate an untouched resume session."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend import main
from backend.models import Conversation, Event, Run
from backend.repository import accept_run, finalize_run
from backend.tests.test_reset_admission import reset_db as reset_db


async def accept(database, conversation_id, mode):
    async with database.session() as session:
        return await accept_run(
            session, conversation_id=conversation_id, client_request_id=str(uuid4()),
            content="Continue", requested_context_mode=mode, owner_id="reset-owner",
        )


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("executor_mode", ["local", "runner"])
async def test_failed_publication_preserves_untouched_resume(reset_db, monkeypatch, cancelled, executor_mode):
    database, conversation_id = reset_db
    accepted = await accept(database, conversation_id, "resume")

    async def executor(*args, **kwargs):
        pytest.fail("No execution may start after publication fails")

    async def deliver(event):
        if cancelled:
            raise asyncio.CancelledError()
        raise ConnectionError("subscriber unavailable")

    monkeypatch.setattr(main, "run_via_runner", executor)
    monkeypatch.setattr(main, "start_claude", executor)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor=executor_mode))
    with pytest.raises(asyncio.CancelledError if cancelled else ConnectionError):
        await main.execute_accepted_run(state, accepted, "Continue", deliver)
    async with database.session() as session:
        run = await session.get(Run, accepted.run_id)
        conversation = await session.get(Conversation, conversation_id)
        assert run.status == ("outcome_unknown" if cancelled else "failed")
        assert (conversation.claude_session_id, conversation.context_state) == ("previous-session", "active")
        terminals = list(await session.scalars(select(Event).where(
            Event.run_id == run.id, Event.type.in_(["run.failed", "run.outcome_unknown"]))))
        assert len(terminals) == 1
        followup = await accept_run(
            session, conversation_id=conversation_id, client_request_id=str(uuid4()),
            content="Resume again", requested_context_mode="resume", owner_id="reset-owner",
        )
        assert followup.resume_session_id == "previous-session"


async def test_failed_reset_publication_does_not_restore_previous_session(reset_db):
    database, conversation_id = reset_db
    accepted = await accept(database, conversation_id, "reset")

    async def deliver(event):
        raise ConnectionError("subscriber unavailable")

    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    with pytest.raises(ConnectionError):
        await main.execute_accepted_run(state, accepted, "Continue", deliver)
    async with database.session() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.claude_session_id is None
        assert conversation.context_state == "unavailable"


async def test_executor_failure_still_invalidates_resume(reset_db, monkeypatch):
    database, conversation_id = reset_db
    accepted = await accept(database, conversation_id, "resume")
    calls = []

    async def executor(app_state, acceptance, content, deliver, *, admission):
        calls.append(acceptance.run_id)
        async with database.session() as session:
            await finalize_run(session, acceptance.run_id, assistant_content=None,
                               status="failed", exit_code=None, error_code="claude_request_failed")

    async def deliver(event):
        pass

    monkeypatch.setattr(main, "run_via_runner", executor)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    await main.execute_accepted_run(state, accepted, "Continue", deliver)
    assert calls == [accepted.run_id]
    async with database.session() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert (conversation.claude_session_id, conversation.context_state) == (None, "unavailable")


async def test_pre_executor_failure_does_not_overwrite_changed_context(reset_db):
    database, conversation_id = reset_db
    accepted = await accept(database, conversation_id, "resume")

    async def deliver(event):
        async with database.session() as session:
            conversation = await session.get(Conversation, conversation_id)
            conversation.claude_session_id = "newer-session"
            conversation.context_state = "active"
            await session.commit()
        raise ConnectionError("subscriber unavailable")

    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    with pytest.raises(ConnectionError):
        await main.execute_accepted_run(state, accepted, "Continue", deliver)
    async with database.session() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert (conversation.claude_session_id, conversation.context_state) == ("newer-session", "active")


@pytest.mark.parametrize("phase", ["cancelled_admission", "post_commit_deadline"])
async def test_pre_executor_admission_interruption_preserves_resume(reset_db, monkeypatch, phase):
    database, conversation_id = reset_db
    accepted = await accept(database, conversation_id, "resume")
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))

    async def executor(*args, **kwargs):
        pytest.fail("Interrupted admission must not invoke an executor")

    monkeypatch.setattr(main, "run_via_runner", executor)
    if phase == "cancelled_admission":
        entered, release = asyncio.Event(), asyncio.Event()
        real_transaction = main.policy_transaction

        async def delayed_transaction(session, operation):
            result = await real_transaction(session, operation)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(main, "policy_transaction", delayed_transaction)
        task = asyncio.create_task(main.execute_accepted_run(state, accepted, "Continue", None))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    else:
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
        monkeypatch.setattr(asyncio, "timeout", lambda delay: original_timeout(0.02 if delay == 30 else delay))
        with pytest.raises(TimeoutError):
            await main.execute_accepted_run(state, accepted, "Continue", None)
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "outcome_unknown"
        conversation = await session.get(Conversation, conversation_id)
        assert (conversation.claude_session_id, conversation.context_state) == ("previous-session", "active")


async def test_duplicate_cannot_finalize_another_resume_owner(reset_db, monkeypatch):
    from backend.ai_access import admit_run, policy_transaction

    database, conversation_id = reset_db
    accepted = await accept(database, conversation_id, "resume")
    async with database.session() as session:
        await policy_transaction(session, lambda: admit_run(session, accepted.run_id))

    async def executor(*args, **kwargs):
        pytest.fail("Duplicate must not invoke an executor")

    async def deliver(event):
        raise ConnectionError("duplicate subscriber unavailable")

    monkeypatch.setattr(main, "run_via_runner", executor)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    await main.execute_accepted_run(state, accepted, "Continue", deliver)
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "running"
        conversation = await session.get(Conversation, conversation_id)
        assert (conversation.claude_session_id, conversation.context_state) == ("previous-session", "active")
