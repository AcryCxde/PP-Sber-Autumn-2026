"""AI-only policy: private normalization, atomic mutation and execution admission."""
import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend.db import Database
from backend.models import AiPolicyAudit, Conversation, Event, Run, User
from backend.tests.databases import database_url


@pytest.fixture
async def policy_db(tmp_path):
    database = Database(database_url(tmp_path / "policy.db"))
    await database.create_schema()
    async with database.session() as session:
        session.add_all([
            User(id="actor", email="admin@test.local", password_hash="hash", role="admin"),
            User(id="target", email="target@test.local", password_hash="hash"),
            User(id="other", email="other@test.local", password_hash="hash"),
            User(id="actor2", email="admin2@test.local", password_hash="hash", role="admin"),
        ])
        await session.commit()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.mark.parametrize("reason", ["", " ", "\t", "\n", " ", " \t\n  "])
async def test_freeze_rejects_unicode_blank_reason(policy_db, reason):
    from backend.ai_access import AiAccessError, change_ai_access

    async with policy_db.session() as session:
        with pytest.raises(AiAccessError) as error:
            await change_ai_access(session, actor_id="actor", target_id="target", frozen=True,
                                   reason=reason, expected_version=0, idempotency_key="test")
        assert error.value.code == "invalid_policy_request"
        await session.rollback()
    async with policy_db.session() as session:
        assert not (await session.get(User, "target")).ai_frozen
        assert await session.scalar(select(func.count()).select_from(AiPolicyAudit)) == 0


async def mutate(database, *, actor="actor", target="target", frozen=True, reason="private", version=0, key="key"):
    from backend.ai_access import change_ai_access, policy_transaction
    async with database.session() as session:
        return await policy_transaction(session, lambda: change_ai_access(session,
            actor_id=actor, target_id=target, frozen=frozen, reason=reason,
            expected_version=version, idempotency_key=key))


async def test_mutation_replay_normalized_body_and_conflicts(policy_db):
    from backend.ai_access import AiAccessError
    first = await mutate(policy_db, reason=" \tprivate ")
    replay = await mutate(policy_db, reason="private")
    assert first == replay
    for kwargs, code in [
        ({"reason": "changed"}, "idempotency_conflict"),
        ({"target": "other"}, "idempotency_conflict"),
        ({"key": "new"}, "policy_version_conflict"),
        ({"key": "new", "version": 1}, "policy_state_conflict"),
    ]:
        with pytest.raises(AiAccessError) as error:
            await mutate(policy_db, **kwargs)
        assert error.value.code == code
    undone = await mutate(policy_db, frozen=False, reason=None, version=1, key="unfreeze")
    assert undone.response["user"]["ai_access"] == {"frozen": False, "version": 2}
    async with policy_db.session() as session:
        rows = list(await session.scalars(select(AiPolicyAudit).order_by(AiPolicyAudit.after_version)))
        assert [row.reason for row in rows] == ["private", None]
        assert len(rows) == 2


@pytest.mark.parametrize("overrides,code", [
    ({"reason": "x" * 1001}, "invalid_policy_request"),
    ({"version": True}, "invalid_policy_request"),
    ({"version": -1}, "invalid_policy_request"),
    ({"key": ""}, "invalid_idempotency_key"),
    ({"target": "actor"}, "self_freeze_forbidden"),
    ({"target": "missing"}, "user_not_found"),
])
async def test_mutation_validation(policy_db, overrides, code):
    from backend.ai_access import AiAccessError
    with pytest.raises(AiAccessError) as error:
        await mutate(policy_db, **overrides)
    assert error.value.code == code


async def test_two_admins_independent_key_space_and_self_unfreeze(policy_db):
    from backend.ai_access import AiAccessError
    await mutate(policy_db, target="actor2", key="shared")
    undone = await mutate(policy_db, actor="actor2", target="actor2", frozen=False,
                          version=1, reason=None, key="shared")
    assert undone.response["user"]["ai_access"] == {"frozen": False, "version": 2}
    with pytest.raises(AiAccessError) as error:
        await mutate(policy_db, actor="target", key="unauthorized")
    assert error.value.code == "admin_required"
    async with policy_db.session() as session:
        assert await session.scalar(select(func.count()).select_from(AiPolicyAudit)) == 2


@pytest.mark.parametrize("invalid", [1, "true", None])
async def test_non_boolean_freeze_rejected(policy_db, invalid):
    from backend.ai_access import AiAccessError
    with pytest.raises(AiAccessError) as error:
        await mutate(policy_db, frozen=invalid)
    assert error.value.code == "invalid_policy_request"


async def test_unfreeze_note_length_boundary(policy_db):
    from backend.ai_access import AiAccessError
    await mutate(policy_db)
    with pytest.raises(AiAccessError):
        await mutate(policy_db, frozen=False, reason="x" * 1001, version=1, key="undo")
    result = await mutate(policy_db, frozen=False, reason="x" * 1000, version=1, key="undo")
    assert result.response["user"]["ai_access"]["frozen"] is False


async def test_reason_length_boundary_and_private_receipt(policy_db):
    result = await mutate(policy_db, reason="x" * 1000)
    assert set(result.response) == {"user", "audit_id", "changed_at"}
    assert set(result.response["user"]) == {"id", "email", "created_at", "last_seen_at", "ai_access"}
    assert set(result.response["user"]["ai_access"]) == {"frozen", "version"}
    assert "reason" not in str(result.response)
    assert result.response["user"]["ai_access"] == {"frozen": True, "version": 1}


async def test_failed_audit_flush_rolls_back_policy(policy_db, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    from backend.ai_access import AiAccessError, change_ai_access, policy_transaction
    async with policy_db.session() as session:
        async def fail_flush(*args, **kwargs):
            raise IntegrityError("insert", {}, ValueError("failure"))
        monkeypatch.setattr(session, "flush", fail_flush)
        with pytest.raises(AiAccessError) as error:
            await policy_transaction(session, lambda: change_ai_access(session, actor_id="actor",
                target_id="target", frozen=True, reason="private", expected_version=0, idempotency_key="x"))
        assert error.value.code == "ai_policy_unavailable"
    async with policy_db.session() as session:
        user = await session.get(User, "target")
        assert (user.ai_frozen, user.ai_policy_version) == (False, 0)
        assert await session.scalar(select(func.count()).select_from(AiPolicyAudit)) == 0


async def pending(database, *, owner="target", context_state="active"):
    async with database.session() as session:
        conversation = Conversation(owner_id=owner, title="safe", context_state=context_state,
                                    claude_session_id="existing-session")
        session.add(conversation)
        await session.flush()
        run = Run(conversation_id=conversation.id, client_request_id=str(uuid4()))
        session.add(run)
        await session.commit()
        return run.id, conversation.id


async def admit(database, run_id):
    from backend.ai_access import admit_run, policy_transaction
    async with database.session() as session:
        return await policy_transaction(session, lambda: admit_run(session, run_id))


async def test_freeze_first_denies_pending_preserves_context(policy_db):
    run_id, conversation_id = await pending(policy_db)
    await mutate(policy_db)
    first = await admit(policy_db, run_id)
    second = await admit(policy_db, run_id)
    assert first.state == "denied" and second.state == "terminal"
    assert first.event.payload["error_code"] == "ai_access_frozen"
    async with policy_db.session() as session:
        run = await session.get(Run, run_id)
        context = await session.get(Conversation, conversation_id)
        assert run.status == "failed" and run.started_at is None and run.admitted_at is None
        assert (context.context_state, context.claude_session_id) == ("active", "existing-session")
        events = list(await session.scalars(select(Event).where(Event.run_id == run_id)))
        assert [event.type for event in events] == ["run.failed"]


async def test_admission_first_survives_freeze_and_completes(policy_db):
    from backend.repository import finalize_run
    run_id, _ = await pending(policy_db)
    assert (await admit(policy_db, run_id)).state == "admitted"
    await mutate(policy_db)
    assert (await admit(policy_db, run_id)).state == "already_admitted"
    async with policy_db.session() as session:
        result = await finalize_run(session, run_id, assistant_content=None,
                                    status="completed", exit_code=0, session_id="existing-session")
        assert result.status == "completed"
    async with policy_db.session() as session:
        run = await session.get(Run, run_id)
        assert run.admitted_at is not None and run.admission_policy_version == 0
        events = list(await session.scalars(select(Event).where(Event.run_id == run_id)))
        assert [event.type for event in events] == ["run.started", "run.completed"]


async def test_missing_owner_and_historical_running_fail_closed(policy_db):
    from backend.ai_access import AiAccessError
    for owner in (None, "target"):
        run_id, _ = await pending(policy_db, owner=owner)
        if owner:
            async with policy_db.session() as session:
                run = await session.get(Run, run_id)
                run.status = "running"
                await session.commit()
        with pytest.raises(AiAccessError) as error:
            await admit(policy_db, run_id)
        assert error.value.code == "ai_policy_unavailable"


async def test_restart_reconciles_historical_running(policy_db):
    from backend.repository import reconcile_orphaned_runs
    run_id, _ = await pending(policy_db)
    async with policy_db.session() as session:
        run = await session.get(Run, run_id)
        run.status = "running"
        await session.commit()
    async with policy_db.session() as session:
        await reconcile_orphaned_runs(session)
    async with policy_db.session() as session:
        run = await session.get(Run, run_id)
        assert run.status == "outcome_unknown" and run.admitted_at is None


async def test_admission_missing_run_is_typed_failure(policy_db):
    from backend.ai_access import AiAccessError
    with pytest.raises(AiAccessError) as error:
        await admit(policy_db, "missing")
    assert (error.value.code, error.value.status_code) == ("run_not_found", 404)


async def test_terminal_run_has_no_new_admission(policy_db):
    run_id, _ = await pending(policy_db)
    async with policy_db.session() as session:
        run = await session.get(Run, run_id)
        run.status = "completed"
        await session.commit()
    assert (await admit(policy_db, run_id)).state == "terminal"
    async with policy_db.session() as session:
        assert (await session.get(Run, run_id)).admitted_at is None
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


async def test_admission_of_one_run_does_not_grant_another(policy_db):
    first, _ = await pending(policy_db)
    second, _ = await pending(policy_db)
    await admit(policy_db, first)
    await mutate(policy_db)
    assert (await admit(policy_db, second)).state == "denied"


async def test_admission_rollback_does_not_grant_execution(policy_db):
    from backend.ai_access import admit_run
    run_id, _ = await pending(policy_db)
    async with policy_db.session() as session:
        assert (await admit_run(session, run_id)).state == "admitted"
        await session.rollback()
    async with policy_db.session() as session:
        run = await session.get(Run, run_id)
        assert run.admitted_at is None and run.status == "pending"
        assert await session.scalar(select(func.count()).select_from(Event)) == 0
    assert (await admit(policy_db, run_id)).state == "admitted"


async def test_policy_transaction_retry_is_bounded(policy_db, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from backend.ai_access import AiAccessError, policy_transaction
    calls = 0
    class Busy(Exception):
        sqlite_errorcode = 5
    async def unavailable():
        nonlocal calls
        calls += 1
        raise OperationalError("update", {}, Busy())
    async with policy_db.session() as session:
        with pytest.raises(AiAccessError) as error:
            await policy_transaction(session, unavailable)
    assert error.value.code == "ai_policy_unavailable" and calls == 3


async def test_real_postgres_lock_wait_is_bounded(policy_db):
    from backend.ai_access import AiAccessError, change_ai_access
    if policy_db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL lock timeout boundary")
    locked = asyncio.Event()
    release = asyncio.Event()
    async def hold():
        async with policy_db.session() as session:
            await change_ai_access(session, actor_id="actor", target_id="target", frozen=True,
                                   reason="hold", expected_version=0, idempotency_key="hold")
            locked.set()
            await release.wait()
            await session.rollback()
    holder = asyncio.create_task(hold())
    await locked.wait()
    try:
        with pytest.raises(AiAccessError) as error:
            await asyncio.wait_for(mutate(policy_db, key="waiter"), timeout=12)
        assert error.value.code == "ai_policy_unavailable"
    finally:
        release.set()
        await holder
    async with policy_db.session() as session:
        assert not (await session.get(User, "target")).ai_frozen
        assert await session.scalar(select(func.count()).select_from(AiPolicyAudit)) == 0


async def test_concurrent_same_key_returns_exact_receipt(policy_db):
    ready = asyncio.Event()
    async def contender():
        await ready.wait()
        return await mutate(policy_db)
    one, two = asyncio.create_task(contender()), asyncio.create_task(contender())
    ready.set()
    first, second = await asyncio.gather(one, two)
    assert first.response == second.response
    async with policy_db.session() as session:
        assert await session.scalar(select(func.count()).select_from(AiPolicyAudit)) == 1


async def test_cross_target_same_actor_key_is_conflict(policy_db):
    from backend.ai_access import AiAccessError
    ready = asyncio.Event()
    async def contender(target):
        await ready.wait()
        try:
            return await mutate(policy_db, target=target)
        except AiAccessError as error:
            return error.code
    one, two = asyncio.create_task(contender("target")), asyncio.create_task(contender("other"))
    ready.set()
    results = await asyncio.gather(one, two)
    assert sum(result == "idempotency_conflict" for result in results) == 1
    async with policy_db.session() as session:
        assert await session.scalar(select(func.count()).select_from(AiPolicyAudit)) == 1


@pytest.mark.parametrize("iteration", range(20))
async def test_concurrent_admissions_have_one_winner(policy_db, iteration):
    del iteration
    run_id, _ = await pending(policy_db)
    ready = asyncio.Event()
    async def contender():
        await ready.wait()
        return await admit(policy_db, run_id)
    one = asyncio.create_task(contender())
    two = asyncio.create_task(contender())
    ready.set()
    results = await asyncio.gather(one, two)
    assert sorted(result.state for result in results) == ["admitted", "already_admitted"]
    async with policy_db.session() as session:
        assert await session.scalar(select(func.count()).select_from(Event).where(Event.type == "run.started")) == 1


@pytest.mark.parametrize("iteration", range(20))
async def test_concurrent_admins_one_version_winner(policy_db, iteration):
    del iteration
    from backend.ai_access import AiAccessError
    ready = asyncio.Event()
    async def contender(key):
        await ready.wait()
        try:
            return await mutate(policy_db, key=key, actor="actor" if key == "one" else "actor2")
        except AiAccessError as error:
            return error.code
    one, two = asyncio.create_task(contender("one")), asyncio.create_task(contender("two"))
    ready.set()
    results = await asyncio.gather(one, two)
    assert sum(result == "policy_version_conflict" for result in results) == 1
    async with policy_db.session() as session:
        assert await session.scalar(select(func.count()).select_from(AiPolicyAudit)) == 1


@pytest.mark.parametrize("freeze_first", [True, False])
@pytest.mark.parametrize("iteration", range(20))
async def test_policy_admission_order_is_commit_order(policy_db, freeze_first, iteration):
    del iteration
    from backend.ai_access import admit_run, change_ai_access
    run_id, _ = await pending(policy_db)
    locked, release, contender_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def winner():
        async with policy_db.session() as session:
            if freeze_first:
                result = await change_ai_access(session, actor_id="actor", target_id="target",
                    frozen=True, reason="private", expected_version=0, idempotency_key="winner")
            else:
                result = await admit_run(session, run_id)
            locked.set()
            await release.wait()
            await session.commit()
            return result
    async def loser():
        await locked.wait()
        contender_started.set()
        if freeze_first:
            return await admit(policy_db, run_id)
        return await mutate(policy_db)
    first, second = asyncio.create_task(winner()), asyncio.create_task(loser())
    await contender_started.wait()
    release.set()
    results = await asyncio.gather(first, second)
    admission = results[1] if freeze_first else results[0]
    assert admission.state == ("denied" if freeze_first else "admitted")
