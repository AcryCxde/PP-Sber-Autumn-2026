"""Reset intent must not destroy context before execution is admitted."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.ai_access import admit_run, policy_transaction
from backend.db import Database
from backend.models import Conversation, Event, Message, Run, User
from backend.repository import accept_run
from backend.tests.databases import database_url


@pytest.fixture
async def reset_db(tmp_path):
    database = Database(database_url(tmp_path / "reset-admission.db"))
    await database.create_schema()
    async with database.session() as session:
        owner = User(id="reset-owner", email="reset@test.local", password_hash="hash")
        session.add(owner)
        await session.flush()
        conversation = Conversation(owner_id=owner.id, title="Existing context",
                                    claude_session_id="previous-session", context_state="active")
        session.add(conversation)
        await session.commit()
        conversation_id = conversation.id
    try:
        yield database, conversation_id
    finally:
        await database.dispose()


async def accept_reset(database, conversation_id, request_id):
    async with database.session() as session:
        return await accept_run(session, conversation_id=conversation_id,
                                client_request_id=request_id, content="Start fresh",
                                requested_context_mode="reset", owner_id="reset-owner")


async def admit(database, run_id):
    async with database.session() as session:
        return await policy_transaction(session, lambda: admit_run(session, run_id))


async def test_reset_denied_after_acceptance_preserves_previous_context(reset_db):
    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    async with database.session() as session:
        owner = await session.get(User, "reset-owner")
        owner.ai_frozen = True
        await session.commit()
    assert (await admit(database, accepted.run_id)).state == "denied"
    async with database.session() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert (conversation.claude_session_id, conversation.context_state) == ("previous-session", "active")
        events = list(await session.scalars(select(Event).where(Event.run_id == accepted.run_id).order_by(Event.seq)))
        assert [event.type for event in events] == ["run.accepted", "run.failed"]
        assert list(await session.scalars(select(Message).where(
            Message.run_id == accepted.run_id, Message.kind == "context_reset"))) == []
        run = await session.get(Run, accepted.run_id)
        assert run.admitted_at is None
        owner = await session.get(User, "reset-owner")
        owner.ai_frozen = False
        await session.commit()
    async with database.session() as session:
        resumed = await accept_run(session, conversation_id=conversation_id,
                                   client_request_id=str(uuid4()), content="Continue",
                                   owner_id="reset-owner")
        assert resumed.context_mode == "resume"
        assert resumed.resume_session_id == "previous-session"


async def test_reset_occurs_once_at_committed_admission_and_replays(reset_db):
    database, conversation_id = reset_db
    request_id = str(uuid4())
    accepted = await accept_reset(database, conversation_id, request_id)
    assert accepted.reset_event is None
    async with database.session() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.claude_session_id == "previous-session"
    first = await admit(database, accepted.run_id)
    second = await admit(database, accepted.run_id)
    assert first.state == "admitted" and second.state == "already_admitted"
    assert [event.type for event in first.context_events] == ["context.reset"]
    assert second.context_events == ()
    replay = await accept_reset(database, conversation_id, request_id)
    assert replay.idempotent_replay and replay.reset_event is not None
    async with database.session() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert (conversation.claude_session_id, conversation.context_state) == (None, "reset")
        events = list(await session.scalars(select(Event).where(Event.run_id == accepted.run_id).order_by(Event.seq)))
        assert [event.type for event in events] == ["run.accepted", "context.reset", "run.started"]
        assert len(list(await session.scalars(select(Message).where(
            Message.run_id == accepted.run_id, Message.kind == "context_reset")))) == 1


async def test_reset_and_admission_roll_back_together(reset_db):
    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    async with database.session() as session:
        assert (await admit_run(session, accepted.run_id)).state == "admitted"
        await session.rollback()
    async with database.session() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert (conversation.claude_session_id, conversation.context_state) == ("previous-session", "active")
        assert list(await session.scalars(select(Event).where(
            Event.run_id == accepted.run_id, Event.type == "context.reset"))) == []
    assert (await admit(database, accepted.run_id)).state == "admitted"


async def test_reset_event_is_delivered_after_commit_before_executor(reset_db, monkeypatch):
    from backend import main

    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    delivered = []
    calls = []

    async def deliver(event):
        async with database.session() as session:
            stored = await session.get(Event, event.id)
            assert stored is not None
            conversation = await session.get(Conversation, conversation_id)
            assert conversation.claude_session_id is None
        delivered.append(event.type)

    async def executor(*args, **kwargs):
        assert delivered == ["context.reset", "run.started"]
        calls.append(kwargs["admission"].run_id)

    monkeypatch.setattr(main, "run_via_runner", executor)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    await main.execute_accepted_run(state, accepted, "Start fresh", deliver)
    await main.execute_accepted_run(state, accepted, "Start fresh", deliver)
    assert calls == [accepted.run_id]
    assert delivered == ["context.reset", "run.started"]


async def test_concurrent_reset_admission_has_one_reset(reset_db):
    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    results = await asyncio.gather(admit(database, accepted.run_id), admit(database, accepted.run_id))
    assert sorted(result.state for result in results) == ["admitted", "already_admitted"]
    async with database.session() as session:
        events = list(await session.scalars(select(Event).where(
            Event.run_id == accepted.run_id).order_by(Event.seq)))
        assert [event.type for event in events] == ["run.accepted", "context.reset", "run.started"]
