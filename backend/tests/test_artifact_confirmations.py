from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.artifact_confirmations import reconcile_artifact_confirmations
from backend.config import Settings
from backend.db import Database
from backend.models import ArtifactConfirmation, Conversation, Project, Run, User
from backend.repository import claim_due_artifact_confirmations, finalize_run
from backend.runner_transport import RunnerTransport, TransportError
from backend.tests.databases import database_url


async def create_run_and_project(database: Database) -> tuple[str, str]:
    async with database.session() as session:
        user = User(email="artifact-confirmation@example.com", password_hash="x")
        conversation = Conversation(title="Artifact confirmation")
        session.add_all([user, conversation])
        await session.flush()
        project = Project(owner_id=user.id, conversation_id=conversation.id)
        run = Run(conversation_id=conversation.id, client_request_id="request-1")
        session.add_all([project, run])
        await session.commit()
        return run.id, project.id


async def add_confirmation(
    database: Database, run_id: str, project_id: str, artifact_id: str, *,
    status: str = "pending", claimed_at: datetime | None = None, attempts: int = 0,
    next_attempt_at: datetime | None = None,
) -> str:
    async with database.session() as session:
        confirmation = ArtifactConfirmation(
            run_id=run_id, project_id=project_id, transport_run_id=run_id, artifact_id=artifact_id,
            source_event_seq=1, version_id="v1",
            receipt={"artifact_id": artifact_id, "version_id": "v1", "size_bytes": 1,
                     "download": "/artifact"},
            sha256="a" * 64, size_bytes=1, status=status, claimed_at=claimed_at,
            attempts=attempts, next_attempt_at=next_attempt_at,
        )
        session.add(confirmation)
        await session.commit()
        return confirmation.id


class RecordingTransport:
    def __init__(self, outcomes: list[BaseException | None] | None = None):
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[str, str, str, dict[str, object]]] = []
        self.abandoned: list[tuple[str, str, str]] = []

    def confirm_saved(self, project: str, run_id: str, artifact_id: str,
                      receipt: dict[str, object]) -> None:
        self.calls.append((project, run_id, artifact_id, receipt))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                raise outcome

    def abandon(self, project: str, run_id: str, code: str) -> None:
        self.abandoned.append((project, run_id, code))


def confirmation_settings(**kwargs: object) -> Settings:
    return Settings(
        artifact_confirmation_batch_size=10, artifact_confirmation_max_attempts=3,
        artifact_confirmation_timeout_seconds=1, artifact_confirmation_lease_seconds=5,
        artifact_confirmation_max_retry_delay_seconds=30, **kwargs,
    )


async def test_confirmation_unique_per_run_and_artifact(tmp_path) -> None:
    database = Database(database_url(tmp_path / "unique.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        await add_confirmation(database, run_id, project_id, "artifact-1")
        async with database.session() as session:
            session.add(ArtifactConfirmation(
                run_id=run_id, project_id=project_id, transport_run_id=run_id, artifact_id="artifact-1",
                source_event_seq=2, version_id="v2", receipt={}, sha256="b" * 64, size_bytes=2,
            ))
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
    finally:
        await database.dispose()


async def test_confirmation_is_deleted_with_its_run(tmp_path) -> None:
    database = Database(database_url(tmp_path / "cascade.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        confirmation_id = await add_confirmation(database, run_id, project_id, "artifact-1")
        async with database.session() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            await session.delete(run)
            await session.commit()
        async with database.session() as session:
            assert await session.get(ArtifactConfirmation, confirmation_id) is None
    finally:
        await database.dispose()


async def test_reconcile_confirms_committed_row_after_recreated_service(tmp_path) -> None:
    database = Database(database_url(tmp_path / "reconcile.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        await add_confirmation(database, run_id, project_id, "artifact-1")
        transport = RecordingTransport()
        result = await reconcile_artifact_confirmations(
            database, transport, confirmation_settings(), now=datetime.now(UTC),
        )
        assert result.confirmed == 1
        assert len(transport.calls) == 1
        async with database.session() as session:
            row = await session.scalar(select(ArtifactConfirmation))
            assert row.status == "confirmed"
    finally:
        await database.dispose()


async def test_confirmed_transport_receipt_is_idempotently_reconciled_after_crash(tmp_path) -> None:
    database = Database(database_url(tmp_path / "post-transport-crash.db"))
    transport_path = tmp_path / "transport.db"
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        await add_confirmation(database, run_id, project_id, "artifact-1")
        transport = RunnerTransport(transport_path)
        transport.message(project_id, run_id, "request-1", "start")
        transport.ingest(project_id, 1, {
            "event_id": "00000000-0000-4000-8000-000000000001", "project_id": project_id,
            "run_id": run_id, "occurred_at": "2026-10-30T00:00:00Z", "type": "artifact.created",
            "payload": {"artifact_id": "artifact-1", "path": "result.txt", "commit_sha": "v1",
                        "role_id": None, "size_bytes": 1, "sha256": "a" * 64},
        })
        transport.confirm_saved(project_id, run_id, "artifact-1", {
            "artifact_id": "artifact-1", "version_id": "v1", "size_bytes": 1,
            "download": "/artifact",
        })
        result = await reconcile_artifact_confirmations(
            database, RunnerTransport(transport_path), confirmation_settings(), now=datetime.now(UTC),
        )
        assert result.confirmed == 1
        assert RunnerTransport(transport_path).events(project_id, run_id)[-1]["type"] == "artifact.saved"
    finally:
        await database.dispose()


async def test_transient_confirmation_failure_is_rescheduled_then_retried(tmp_path) -> None:
    database = Database(database_url(tmp_path / "retry.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        await add_confirmation(database, run_id, project_id, "artifact-1")
        start = datetime.now(UTC)
        first = await reconcile_artifact_confirmations(
            database, RecordingTransport([TransportError("unknown_artifact")]),
            confirmation_settings(), now=start,
        )
        assert first.rescheduled == 1
        async with database.session() as session:
            row = await session.scalar(select(ArtifactConfirmation))
            assert (row.status, row.attempts, row.error_category) == ("pending", 1, "transport")
            retry_at = row.next_attempt_at
        second = await reconcile_artifact_confirmations(
            database, RecordingTransport(), confirmation_settings(), now=retry_at,
        )
        assert second.confirmed == 1
    finally:
        await database.dispose()


async def test_conflict_is_durable_and_not_retried(tmp_path) -> None:
    database = Database(database_url(tmp_path / "conflict.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        await add_confirmation(database, run_id, project_id, "artifact-1")
        result = await reconcile_artifact_confirmations(
            database, RecordingTransport([TransportError("storage_receipt_conflict")]),
            confirmation_settings(), now=datetime.now(UTC),
        )
        assert result.conflicted == 1
        async with database.session() as session:
            row = await session.scalar(select(ArtifactConfirmation))
            assert (row.status, row.error_category) == ("conflicted", "transport_conflict")
    finally:
        await database.dispose()


async def test_conflict_finalizes_owning_active_run_unknown(tmp_path) -> None:
    database = Database(database_url(tmp_path / "conflict-run.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        await add_confirmation(database, run_id, project_id, "artifact-1")
        result = await reconcile_artifact_confirmations(
            database,
            RecordingTransport([TransportError("storage_receipt_conflict")]),
            confirmation_settings(),
            now=datetime.now(UTC),
        )
        assert len(result.terminal_events) == 1
        async with database.session() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            assert (run.status, run.error_code) == ("outcome_unknown", "artifact_confirmation_failed")
    finally:
        await database.dispose()


async def test_attempt_exhaustion_notifies_the_waiting_runner_relay(tmp_path) -> None:
    database = Database(database_url(tmp_path / "exhaustion-relay.db"))
    transport = RunnerTransport(tmp_path / "transport.db")
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        await add_confirmation(database, run_id, project_id, "artifact-1", attempts=2)
        transport.message(project_id, run_id, "request-1", "start")
        transport.ingest(project_id, 1, {
            "event_id": "00000000-0000-4000-8000-000000000003",
            "project_id": project_id,
            "run_id": run_id,
            "occurred_at": "2026-10-30T00:00:00Z",
            "type": "artifact.created",
            "payload": {
                "artifact_id": "artifact-1", "path": "result.txt", "commit_sha": "v1",
                "role_id": None, "size_bytes": 1, "sha256": "a" * 64,
            },
        })

        def unavailable(*args: object) -> None:
            raise TransportError("unknown_artifact")

        transport.confirm_saved = unavailable  # type: ignore[method-assign]
        await reconcile_artifact_confirmations(
            database, transport, confirmation_settings(), now=datetime.now(UTC),
        )

        assert transport.status(project_id, run_id)["status"] == "outcome_unknown"
    finally:
        await database.dispose()


async def test_terminal_confirmation_failure_notifies_the_waiting_runner_relay(tmp_path) -> None:
    database = Database(database_url(tmp_path / "conflict-relay.db"))
    transport = RunnerTransport(tmp_path / "transport.db")
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        await add_confirmation(database, run_id, project_id, "artifact-1")
        transport.message(project_id, run_id, "request-1", "start")
        transport.ingest(project_id, 1, {
            "event_id": "00000000-0000-4000-8000-000000000001",
            "project_id": project_id,
            "run_id": run_id,
            "occurred_at": "2026-10-30T00:00:00Z",
            "type": "artifact.created",
            "payload": {
                "artifact_id": "artifact-1", "path": "result.txt", "commit_sha": "v1",
                "role_id": None, "size_bytes": 1, "sha256": "a" * 64,
            },
        })

        def conflict(*args: object) -> None:
            raise TransportError("storage_receipt_conflict")

        transport.confirm_saved = conflict  # type: ignore[method-assign]
        result = await reconcile_artifact_confirmations(
            database, transport, confirmation_settings(), now=datetime.now(UTC),
        )

        assert len(result.terminal_events) == 1
        assert transport.status(project_id, run_id)["status"] == "outcome_unknown"
        assert transport.events(project_id, run_id)[-1]["payload"] == {
            "code": "artifact_confirmation_failed",
        }
    finally:
        await database.dispose()


async def test_terminal_confirmation_failure_abandons_transport_for_terminal_backend_run(
    tmp_path,
) -> None:
    database = Database(database_url(tmp_path / "terminal-conflict.db"))
    transport = RunnerTransport(tmp_path / "terminal-transport.db")
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        await add_confirmation(database, run_id, project_id, "artifact-1")
        transport.message(project_id, run_id, "request-1", "start")
        transport.ingest(project_id, 1, {
            "event_id": "00000000-0000-4000-8000-000000000001",
            "project_id": project_id,
            "run_id": run_id,
            "occurred_at": "2026-10-30T00:00:00Z",
            "type": "artifact.created",
            "payload": {
                "artifact_id": "artifact-1", "path": "result.txt", "commit_sha": "v1",
                "role_id": None, "size_bytes": 1, "sha256": "a" * 64,
            },
        })
        async with database.session() as session:
            await finalize_run(
                session,
                run_id,
                assistant_content=None,
                status="outcome_unknown",
                exit_code=None,
                session_id=None,
                error_code="artifact_confirmation_pending",
            )

        def conflict(*args: object) -> None:
            raise TransportError("storage_receipt_conflict")

        transport.confirm_saved = conflict  # type: ignore[method-assign]
        result = await reconcile_artifact_confirmations(
            database, transport, confirmation_settings(), now=datetime.now(UTC),
        )

        assert result.terminal_events == ()
        assert transport.status(project_id, run_id)["status"] == "outcome_unknown"
        assert transport.active_run(project_id) is None
    finally:
        await database.dispose()


async def test_concurrent_claims_are_disjoint_and_stale_claim_is_recovered(tmp_path) -> None:
    database = Database(database_url(tmp_path / "claims.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        now = datetime.now(UTC)
        await add_confirmation(database, run_id, project_id, "artifact-1")
        async with database.session() as first_session:
            first = await claim_due_artifact_confirmations(
                first_session, now=now, limit=1, lease_seconds=5, max_attempts=3,
            )
        async with database.session() as second_session:
            second = await claim_due_artifact_confirmations(
                second_session, now=now, limit=1, lease_seconds=5, max_attempts=3,
            )
        assert len(first) == 1
        assert second == []
        async with database.session() as recovery_session:
            recovered = await claim_due_artifact_confirmations(
                recovery_session, now=now + timedelta(seconds=6), limit=1,
                lease_seconds=5, max_attempts=3,
            )
        assert len(recovered) == 1
        assert recovered[0].attempts == 2
    finally:
        await database.dispose()


async def test_batch_completion_and_attempt_cap(tmp_path) -> None:
    database = Database(database_url(tmp_path / "batch.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_run_and_project(database)
        await add_confirmation(database, run_id, project_id, "artifact-1")
        await add_confirmation(database, run_id, project_id, "artifact-2")
        result = await reconcile_artifact_confirmations(
            database, RecordingTransport(), confirmation_settings(), now=datetime.now(UTC),
        )
        assert result.confirmed == 2
        capped_id = await add_confirmation(database, run_id, project_id, "artifact-3", attempts=2)
        capped = await reconcile_artifact_confirmations(
            database, RecordingTransport([TransportError("unknown_artifact")]),
            confirmation_settings(), now=datetime.now(UTC),
        )
        assert capped.conflicted == 1
        async with database.session() as session:
            row = await session.get(ArtifactConfirmation, capped_id)
            assert (row.status, row.error_category) == ("conflicted", "attempts_exhausted")
    finally:
        await database.dispose()
