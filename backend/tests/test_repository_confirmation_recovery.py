"""Confirmation recovery and ownership regressions, including real PostgreSQL locks."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from backend.db import Database
from backend.models import ArtifactConfirmation, Conversation, Event, Run
from backend.repository import (
    accept_run,
    claim_due_artifact_confirmations,
    create_request,
    finalize_artifact_confirmation_success,
    finalize_run,
    mark_artifact_confirmation_confirmed,
    mark_artifact_confirmation_conflicted,
    mark_run_running,
    reschedule_artifact_confirmation,
    reconcile_orphaned_runs,
    stage_runner_artifact,
)
from backend.tests.databases import POSTGRES_URL, database_url


@pytest.fixture
async def database(tmp_path):
    db = Database(database_url(tmp_path / "confirmation-recovery.db"))
    await db.create_schema()
    try:
        yield db
    finally:
        await db.dispose()


async def stage(database, run_id, artifact_id="artifact-1"):
    import hashlib

    async with database.session() as session:
        await stage_runner_artifact(
            session, run_id, project_id="project-1", transport_run_id=run_id,
            artifact_id=artifact_id, path=f"{artifact_id}.md", content="hello",
            source_bytes=b"hello", version_id=artifact_id,
            sha256=hashlib.sha256(b"hello").hexdigest(),
        )
        await session.commit()


async def pending_completed_run(database, count=1):
    async with database.session() as session:
        created = await create_request(session, "original")
    for index in range(count):
        await stage(database, created.run_id, f"artifact-{index}")
    async with database.session() as session:
        await finalize_run(
            session, created.run_id, assistant_content="done", status="completed",
            exit_code=0, session_id="original-session",
        )
    async with database.session() as session:
        claims = await claim_due_artifact_confirmations(
            session, now=datetime.now(UTC), limit=count, lease_seconds=60, max_attempts=3,
        )
    return created, claims


async def confirm(database, claim):
    async with database.session() as session:
        assert await mark_artifact_confirmation_confirmed(session, claim)
        event = await finalize_artifact_confirmation_success(session, claim.run_id, commit=False)
        await session.commit()
        return event


@pytest.mark.parametrize("claimed", [False, True])
async def test_restart_finalizes_execution_without_discarding_delivery(database, claimed):
    async with database.session() as session:
        created = await create_request(session, "interrupted")
        await mark_run_running(session, created.run_id)
    await stage(database, created.run_id)
    async with database.session() as session:
        claims = await claim_due_artifact_confirmations(
            session, now=datetime.now(UTC), limit=1, lease_seconds=60, max_attempts=3,
        ) if claimed else []
    async with database.session() as session:
        events = await reconcile_orphaned_runs(session)
        assert len(events) == 1
        assert events[0].type == "run.outcome_unknown"
        assert events[0].payload["error_code"] == "backend_restarted"
        assert await reconcile_orphaned_runs(session) == []
    async with database.session() as session:
        run = await session.get(Run, created.run_id)
        row = await session.scalar(select(ArtifactConfirmation))
        assert (run.status, run.error_code) == ("outcome_unknown", "backend_restarted")
        assert row.status == ("confirming" if claimed else "pending")
        assert await finalize_artifact_confirmation_success(session, created.run_id) is None
    if not claims:
        async with database.session() as session:
            claims = await claim_due_artifact_confirmations(
                session, now=datetime.now(UTC), limit=1, lease_seconds=60, max_attempts=3,
            )
    assert await confirm(database, claims[0]) is None
    async with database.session() as session:
        assert (await session.get(Run, created.run_id)).error_code == "backend_restarted"
        assert (await session.scalar(select(ArtifactConfirmation))).status == "confirmed"
        # The unknown execution does not block an explicit reset.
        await accept_run(
            session, conversation_id=created.conversation_id, client_request_id=str(uuid4()),
            content="reset after restart", requested_context_mode="reset",
        )


@pytest.mark.parametrize("newer_completed", [False, True])
async def test_delayed_success_does_not_restore_superseded_context(database, newer_completed):
    created, claims = await pending_completed_run(database)
    async with database.session() as session:
        newer = await accept_run(
            session, conversation_id=created.conversation_id, client_request_id=str(uuid4()),
            content="new owner", requested_context_mode="reset",
        )
        if newer_completed:
            await finalize_run(
                session, newer.run_id, assistant_content="new result", status="completed",
                exit_code=0, session_id="new-session",
            )
    event = await confirm(database, claims[0])
    assert event.type == "run.completed"
    async with database.session() as session:
        conversation = await session.get(Conversation, created.conversation_id)
        expected = ("new-session", "active") if newer_completed else (None, "reset")
        assert (conversation.claude_session_id, conversation.context_state) == expected
        assert (await session.get(Run, created.run_id)).status == "completed"


async def test_current_owner_success_restores_context_once(database):
    created, claims = await pending_completed_run(database)
    assert (await confirm(database, claims[0])).type == "run.completed"
    async with database.session() as session:
        assert await finalize_artifact_confirmation_success(session, created.run_id) is None
        conversation = await session.get(Conversation, created.conversation_id)
        assert (conversation.claude_session_id, conversation.context_state) == ("original-session", "active")
        completed = list(await session.scalars(select(Event).where(Event.type == "run.completed")))
        assert len(completed) == 1


@pytest.mark.skipif(not POSTGRES_URL, reason="requires real PostgreSQL row locking")
@pytest.mark.parametrize("transition", ["confirmed", "conflicted", "rescheduled"])
async def test_concurrent_last_confirmations_serialize_before_child_mutation(database, transition):
    created, claims = await pending_completed_run(database, count=2)
    # A parent row held by another transaction must block BOTH child transitions.
    # PostgreSQL lock_timeout makes this deterministic without scheduling sleeps.
    async with database.session() as blocker:
        await blocker.scalar(select(Run).where(Run.id == created.run_id).with_for_update())

        async def attempt(claim):
            from sqlalchemy.exc import DBAPIError

            async with database.session() as session:
                await session.execute(text("SET LOCAL lock_timeout = '200ms'"))
                with pytest.raises(DBAPIError, match="lock timeout"):
                    if transition == "confirmed":
                        await mark_artifact_confirmation_confirmed(session, claim)
                    elif transition == "conflicted":
                        await mark_artifact_confirmation_conflicted(session, claim, category="transport_conflict")
                    else:
                        await reschedule_artifact_confirmation(
                            session, claim, category="timeout", next_attempt_at=datetime.now(UTC),
                        )
                await session.rollback()

        await asyncio.wait_for(asyncio.gather(*(attempt(claim) for claim in claims)), timeout=5)
        await blocker.rollback()

    results = await asyncio.wait_for(asyncio.gather(*(confirm(database, claim) for claim in claims)), timeout=5)
    assert sum(event is not None for event in results) == 1
    async with database.session() as session:
        assert (await session.get(Run, created.run_id)).status == "completed"
        statuses = list(await session.scalars(select(ArtifactConfirmation.status)))
        assert statuses == ["confirmed", "confirmed"]
        events = list(await session.scalars(select(Event).where(Event.type == "run.completed")))
        assert len(events) == 1
