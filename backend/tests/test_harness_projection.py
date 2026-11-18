"""Versioned snapshots are diagnostic session totals, never Run cost."""
from uuid import uuid4

import pytest

from backend.db import Database
from backend.models import Conversation, Run, User
from backend.harness_estimate import catch_up, safe_estimate, wrapper


def test_harness_wrapper_pinned_old_mapper_unknown():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    from pathlib import Path
    path = Path(__file__).parent / "fixtures/harness/safe_estimate_pre_task6.py.txt"
    loader = SourceFileLoader("pinned_estimate", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    stored = wrapper(observation())
    assert module.safe_estimate(stored) is None
    assert safe_estimate(stored)["scope"] == "session_total"
    assert module.safe_estimate({**stored["estimate"], "lineage": stored["lineage"]}) is not None


def observation(order=2, value="0.062745"):
    source = str(uuid4())
    return {"frame": {"diagnostic_id": str(uuid4()), "source_event_id": source,
             "payload": {"session_ref": "fd81d33f-1427-424b-a030-0065468064f7",
                         "observed_at": "2026-11-12T00:00:00Z", "value_usd": value}},
            "lineage": {"project": "p1", "transport_run": "run-1", "command_id": "run-1"},
            "source_order": {"public_seq": order, "journal_seq": order}}


async def setup(database):
    await database.create_schema()
    async with database.session() as session:
        user = User(email="diagnostic@example.test", password_hash="fake")
        session.add(user)
        await session.flush()
        conversation = Conversation(owner_id=user.id, title="private")
        session.add(conversation)
        await session.flush()
        run = Run(conversation_id=conversation.id, client_request_id=str(uuid4()), status="completed")
        session.add(run)
        await session.commit()
        return run.id


@pytest.mark.asyncio
async def test_harness_projection_monotonic_two_sessions(tmp_path):
    from backend.tests.databases import database_url
    database = Database(database_url(tmp_path / "backend.db"))
    run_id = await setup(database)
    s1, s2 = observation(2, "0.9"), observation(3, "0.1")
    # A captured S1 before B committed S2. A must compare against fresh persisted state.
    import asyncio
    captured = asyncio.Event()
    release = asyncio.Event()
    async def delayed_a():
        captured.set()
        await release.wait()
        await catch_up(database, run_id, [s1])
    a = asyncio.create_task(delayed_a())
    await captured.wait()
    await catch_up(database, run_id, [s2])
    release.set()
    await a
    async with database.session() as session:
        run = await session.get(Run, run_id)
        assert safe_estimate(run.harness_estimate)["value_usd"] == "0.1"
        assert "scope" not in run.harness_estimate
        assert run.cost_usd is None
    await database.dispose()


def test_admin_detail_recovers_committed_receipt_without_projection(tmp_path):
    from fastapi.testclient import TestClient
    from backend.runner_transport import RunnerTransport
    from backend.tests.test_history_api import make_app
    from backend.tests.test_admin_operations import seed
    from backend.tests.auth_helpers import sign_in
    from backend.tests.test_harness_diagnostics import usage, frame
    app = make_app(tmp_path)
    transport = RunnerTransport(tmp_path / "transport.db")
    app.state.runner_transport = transport
    with TestClient(app) as client:
        sign_in(client)
        run_id = seed(app, client)[0]
        transport.message("p1", "run-1", "client", "go", backend_run_id=run_id)
        source = usage()
        transport.ingest("p1", 2, source)
        transport.diagnostic("p1", frame(source))
        # Simulate crash after durable private receipt and before any backend projection.
        body = client.get(f"/api/admin/v1/runs/{run_id}").json()
        assert body["harness_estimate"]["value_usd"] == "0.062745"
        assert body["harness_estimate"]["scope"] == "session_total"
        assert client.get(f"/api/admin/v1/runs/{run_id}").json()["harness_estimate"] == body["harness_estimate"]


@pytest.mark.asyncio
async def test_projection_simultaneous_writers_use_fresh_persisted_order(tmp_path):
    import asyncio
    from backend.tests.databases import database_url
    database = Database(database_url(tmp_path / "concurrent.db"))
    run_id = await setup(database)
    low, high = observation(2), observation(3, "0.001")
    await asyncio.gather(catch_up(database, run_id, [low]), catch_up(database, run_id, [high]))
    async with database.session() as session:
        stored = (await session.get(Run, run_id)).harness_estimate
        assert stored["diagnostic_id"] == high["frame"]["diagnostic_id"]
    await database.dispose()


@pytest.mark.asyncio
async def test_conflicting_lineage_never_becomes_a_new_known_estimate(tmp_path):
    from backend.tests.databases import database_url
    database = Database(database_url(tmp_path / "backend.db"))
    run_id = await setup(database)
    first, other = observation(), observation(3)
    other["lineage"]["command_id"] = "other"
    await catch_up(database, run_id, [first])
    with pytest.raises(ValueError, match="harness_lineage_conflict"):
        await catch_up(database, run_id, [other])
    async with database.session() as session:
        assert (await session.get(Run, run_id)).harness_estimate["diagnostic_id"] == first["frame"]["diagnostic_id"]
    await database.dispose()
