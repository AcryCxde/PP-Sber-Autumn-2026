"""Admission publication failures and admin UTC serialization regressions."""
import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import main
from backend.models import Event, Run, User
from backend.repository import accept_run
from backend.tests.auth_helpers import sign_in
from backend.tests.test_admin_api import make_app
from backend.tests.test_reset_admission import accept_reset, reset_db as reset_db


@pytest.mark.parametrize("executor_mode", ["runner", "local"])
@pytest.mark.parametrize("event_type", ["context.reset", "run.started"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_admission_delivery_failure_persists_terminal(
    reset_db, monkeypatch, event_type, cancelled, executor_mode,
):
    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    executor_calls = []

    async def executor(*args, **kwargs):
        executor_calls.append(accepted.run_id)

    async def deliver(event):
        if event.type == event_type:
            if cancelled:
                raise asyncio.CancelledError()
            raise ConnectionError("subscriber delivery failed")

    monkeypatch.setattr(main, "run_via_runner", executor)
    monkeypatch.setattr(main, "start_claude", executor)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor=executor_mode))
    expected = asyncio.CancelledError if cancelled else ConnectionError
    with pytest.raises(expected):
        await main.execute_accepted_run(state, accepted, "Start fresh", deliver)
    assert executor_calls == []
    async with database.session() as session:
        run = await session.get(Run, accepted.run_id)
        assert run.status == ("outcome_unknown" if cancelled else "failed")
        terminals = list(await session.scalars(select(Event).where(
            Event.run_id == run.id, Event.type.in_(["run.failed", "run.outcome_unknown"]))))
        assert len(terminals) == 1
        followup = await accept_run(session, conversation_id=conversation_id,
            client_request_id=str(uuid4()), content="Try again", owner_id="reset-owner",
            requested_context_mode="reset")
        assert followup.run_id != run.id
    await main.execute_accepted_run(state, accepted, "Start fresh", lambda event: None)
    assert executor_calls == []


async def test_actual_task_cancellation_during_publication_is_persisted(reset_db, monkeypatch):
    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    publication_started = asyncio.Event()
    never = asyncio.Event()

    async def deliver(event):
        publication_started.set()
        await never.wait()

    async def executor(*args, **kwargs):
        pytest.fail("Executor must not start after cancelled publication")

    monkeypatch.setattr(main, "run_via_runner", executor)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    task = asyncio.create_task(main.execute_accepted_run(state, accepted, "Start fresh", deliver))
    await asyncio.wait_for(publication_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    async with database.session() as session:
        run = await session.get(Run, accepted.run_id)
        assert run.status == "outcome_unknown"
        assert run.error_code == "heartbeat_lost"


async def test_repeated_cancellation_waits_for_terminal_commit(reset_db, monkeypatch):
    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    publishing = asyncio.Event()
    finalizing = asyncio.Event()
    release = asyncio.Event()
    real_finalize = main.finalize_run

    async def blocked_finalize(*args, **kwargs):
        finalizing.set()
        await release.wait()
        return await real_finalize(*args, **kwargs)

    async def deliver(event):
        publishing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "finalize_run", blocked_finalize)
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    task = asyncio.create_task(main.execute_accepted_run(state, accepted, "Start fresh", deliver))
    await asyncio.wait_for(publishing.wait(), 2)
    task.cancel()
    await asyncio.wait_for(finalizing.wait(), 2)
    task.cancel()
    # A barrier queued after cancellation processing observes whether the owner
    # detached its still-uncommitted finalization task.
    observed = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(
        lambda: asyncio.get_running_loop().call_soon(observed.set_result, task.done()))
    assert await observed is False
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    async with database.session() as session:
        assert (await session.get(Run, accepted.run_id)).status == "outcome_unknown"


@pytest.mark.parametrize("failure", ["database", "deadline"])
async def test_publication_finalization_failure_is_not_silent(reset_db, monkeypatch, failure):
    database, conversation_id = reset_db
    accepted = await accept_reset(database, conversation_id, str(uuid4()))
    original_timeout = asyncio.timeout
    cleanup_finished = asyncio.Event()

    async def broken_finalize(*args, **kwargs):
        try:
            if failure == "database":
                raise RuntimeError("database unavailable")
            await asyncio.Event().wait()
        finally:
            cleanup_finished.set()

    async def deliver(event):
        raise ConnectionError("subscriber unavailable")

    monkeypatch.setattr(main, "finalize_run", broken_finalize)
    if failure == "deadline":
        monkeypatch.setattr(asyncio, "timeout", lambda delay: original_timeout(0.001 if delay == 10 else delay))
    state = SimpleNamespace(database=database, settings=SimpleNamespace(executor="runner"))
    expected = RuntimeError if failure == "database" else TimeoutError
    with pytest.raises(expected) as caught:
        await main.execute_accepted_run(state, accepted, "Start fresh", deliver)
    assert cleanup_finished.is_set()
    assert isinstance(caught.value.__context__, ConnectionError)


def test_utc_serializer_normalizes_aware_offsets():
    from backend.timestamps import utc_iso

    original = datetime(2026, 11, 12, 15, tzinfo=timezone(timedelta(hours=3)))
    assert utc_iso(original) == "2026-11-12T12:00:00+00:00"


def test_admin_users_audit_and_receipt_timestamps_are_utc(tmp_path):
    app = make_app(tmp_path)
    app.state.settings.admin_policy_management_enabled = True
    stored = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    with TestClient(app) as client:
        target = sign_in(client, "utc-target@example.test")
        client.cookies.clear()
        operator = sign_in(client, "utc-admin@example.test")

        async def seed():
            async with app.state.database.session() as session:
                admin = await session.get(User, operator["id"])
                admin.role = "admin"
                user = await session.get(User, target["id"])
                user.created_at = stored
                user.last_seen_at = stored
                await session.commit()

        client.portal.call(seed)
        listed = client.get("/api/admin/v1/users", params={"q": "utc-target"}).json()["items"][0]
        path = f"/api/admin/v1/users/{target['id']}/ai-access"
        headers = {"X-CoreTeams-Admin": "1", "Idempotency-Key": "utc-freeze"}
        body = {"frozen": True, "reason": "Test", "expected_version": 0}
        response = client.patch(path, json=body, headers=headers)
        assert response.status_code == 200
        receipt = response.json()
        assert client.patch(path, json=body, headers=headers).json() == receipt
        audit = client.get(f"/api/admin/v1/users/{target['id']}/audit").json()["items"][0]
        for value in (listed["created_at"], listed["last_seen_at"],
                      receipt["user"]["created_at"], receipt["user"]["last_seen_at"],
                      receipt["changed_at"], audit["created_at"]):
            parsed = datetime.fromisoformat(value)
            assert parsed.utcoffset() == timedelta(0)
        assert datetime.fromisoformat(listed["created_at"]).astimezone(
            timezone(timedelta(hours=3))).hour == 15
