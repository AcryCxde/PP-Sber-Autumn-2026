import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

import backend.main as main
from backend.artifact_confirmations import reconcile_artifact_confirmations
from backend.config import Settings
from backend.db import Database
from backend.models import ArtifactConfirmation, Run, User
from backend.repository import finalize_run
from backend.runner_transport import TransportError
from backend.tests.databases import database_url
from backend.tests.test_artifact_confirmations import (
    RecordingTransport,
    add_confirmation,
    confirmation_settings,
    create_run_and_project,
)


@pytest.fixture
async def database(tmp_path):
    database = Database(database_url(tmp_path / "lifecycle.db"))
    await database.create_schema()
    try:
        yield database
    finally:
        await database.dispose()


class Socket:
    def __init__(self, app):
        self.app = app
        self.events = []
        self.queue = asyncio.Queue()

    async def send_text(self, raw):
        event = json.loads(raw)
        self.events.append(event)
        await self.queue.put(event)

    async def until(self, kind):
        async with asyncio.timeout(5):
            while True:
                event = await self.queue.get()
                if event["type"] == kind:
                    return event


async def test_queued_confirmation_does_not_spend_lease_or_attempt(database, monkeypatch):
    run_id, project_id = await create_run_and_project(database)
    first = await add_confirmation(database, run_id, project_id, "first")
    second = await add_confirmation(database, run_id, project_id, "second")
    observed = []
    import backend.artifact_confirmations as service
    original = service.mark_artifact_confirmation_confirmed

    async def inspect_queue(session, claim):
        if not observed:
            queued = await session.get(ArtifactConfirmation, second if claim.id == first else first)
            observed.append((queued.status, queued.attempts, queued.claimed_at))
        return await original(session, claim)

    monkeypatch.setattr(service, "mark_artifact_confirmation_confirmed", inspect_queue)
    result = await reconcile_artifact_confirmations(database, RecordingTransport(), confirmation_settings())
    assert observed == [("pending", 0, None)]
    assert (result.claimed, result.confirmed) == (2, 2)


async def test_abandon_backlog_precedes_confirmation_lease(database, monkeypatch):
    run_id, project_id = await create_run_and_project(database)
    pending = await add_confirmation(database, run_id, project_id, "pending")
    await add_confirmation(database, run_id, project_id, "abandon", status="conflicted")
    import backend.artifact_confirmations as service
    original = service._deliver_terminal_abandon
    observed = []

    async def inspect_abandon(database, transport, claim, **kwargs):
        async with database.session() as session:
            queued = await session.get(ArtifactConfirmation, pending)
            observed.append((queued.status, queued.attempts))
        await original(database, transport, claim, **kwargs)

    monkeypatch.setattr(service, "_deliver_terminal_abandon", inspect_abandon)
    await reconcile_artifact_confirmations(database, RecordingTransport(), confirmation_settings())
    assert observed == [("pending", 0)]


@pytest.mark.parametrize("correct_before_handoff", [False, True])
async def test_pending_terminal_gets_bounded_correction_subscription(
    database, monkeypatch, correct_before_handoff,
):
    async with database.session() as session:
        user = User(email="lifecycle@example.com", password_hash="x")
        session.add(user)
        await session.commit()
        owner_id = user.id
    settings = Settings(executor="runner", subscription_window_seconds=1,
                        subscription_poll_interval_seconds=0.001)
    app = main.create_app(database, settings)
    socket = Socket(app)

    async def pending_runner(state, acceptance, content, deliver):
        await add_confirmation(database, acceptance.run_id, acceptance.conversation_id, "artifact")
        async with database.session() as session:
            final = await finalize_run(session, acceptance.run_id, status="completed", session_id="runner", assistant_content=None, exit_code=None)
        await deliver(final.terminal_event)
        if correct_before_handoff:
            # Commit without live publication, before the subscription exists.
            await reconcile_artifact_confirmations(database, RecordingTransport(), confirmation_settings())

    monkeypatch.setattr(main, "run_via_runner", pending_runner)
    await main.handle_run_create(socket, conversation_id=None, client_request_id=str(uuid4()),
                                 content="hello", context_mode="resume", owner_id=owner_id)
    pending = socket.events[-1]
    assert pending.get("error_code") == "artifact_confirmation_pending", socket.events
    try:
        if not correct_before_handoff:
            result = await reconcile_artifact_confirmations(database, RecordingTransport(), confirmation_settings())
            assert len(result.terminal_events) == 1
            await main.publish_live_terminal_event(app, result.terminal_events[0])
        correction = await socket.until("run.completed")
        assert correction["run_id"] == pending["run_id"]
        assert correction["seq"] > pending["seq"]
        complete = await socket.until("replay.complete")
        assert complete["terminal"] is True
        assert [event["seq"] for event in socket.events if "seq" in event] == [1, 2, 3]
        assert app.state.live_run_deliveries == {}
    finally:
        tasks = list(getattr(socket, "_gateway_subscriptions", {}).values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_reconnect_pending_replay_is_nonterminal_and_bounded(database):
    run_id, project_id = await create_run_and_project(database)
    await add_confirmation(database, run_id, project_id, "pending")
    async with database.session() as session:
        run = await session.get(Run, run_id)
        owner = await session.scalar(select(User))
        from backend.models import Conversation
        conversation = await session.get(Conversation, run.conversation_id)
        conversation.owner_id = owner.id
        await session.commit()
        await finalize_run(session, run_id, status="completed", session_id="runner", assistant_content=None, exit_code=None)
        owner_id = owner.id
    socket = Socket(main.create_app(database, Settings(subscription_window_seconds=0.001,
                                                      subscription_poll_interval_seconds=0.001)))
    await asyncio.wait_for(main.handle_subscription(socket, run_id, 0, owner_id=owner_id), 5)
    assert socket.events[-1]["type"] == "replay.complete"
    assert socket.events[-1]["terminal"] is False


async def test_reconcile_does_not_reclaim_rescheduled_row_in_same_pass(database, monkeypatch):
    run_id, project_id = await create_run_and_project(database)
    await add_confirmation(database, run_id, project_id, "retry")
    import backend.artifact_confirmations as service

    class AdvancingClock:
        tick = datetime.now(UTC)

        @classmethod
        def now(cls, timezone):
            cls.tick += timedelta(seconds=30)
            return cls.tick

    monkeypatch.setattr(service, "datetime", AdvancingClock)
    transport = RecordingTransport([TransportError("unavailable")] * 3)
    result = await reconcile_artifact_confirmations(database, transport, confirmation_settings())
    assert (result.claimed, result.rescheduled, result.conflicted) == (1, 1, 0)
    assert len(transport.calls) == 1


async def test_subscription_replacement_and_disconnect_prevent_late_task(database, monkeypatch):
    socket = Socket(main.create_app(database))
    entered = asyncio.Queue()
    cancelled = asyncio.Queue()

    async def blocked_subscription(websocket, run_id, after_seq, *, owner_id):
        await entered.put((run_id, after_seq))
        try:
            await asyncio.Future()
        finally:
            await cancelled.put(run_id)

    monkeypatch.setattr(main, "handle_subscription", blocked_subscription)
    main.start_subscription_task(socket, "first", 2, owner_id="owner")
    await asyncio.wait_for(entered.get(), 5)
    previous = socket._gateway_subscriptions["first"]
    main.start_subscription_task(socket, "first", 3, owner_id="owner")
    await asyncio.wait_for(cancelled.get(), 5)
    assert await asyncio.wait_for(entered.get(), 5) == ("first", 3)
    assert previous.cancelled()
    active = socket._gateway_subscriptions["first"]
    socket._gateway_disconnected = True
    main.start_subscription_task(socket, "late", 0, owner_id="owner")
    assert "late" not in socket._gateway_subscriptions
    active.cancel()
    await asyncio.gather(active, return_exceptions=True)
    assert socket._gateway_subscriptions == {}
