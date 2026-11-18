"""Optional diagnostics must not break core reads/delivery or rewrite stable data."""
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from backend.db import Database
from backend.harness_estimate import catch_up
from backend.runner_transport import RunnerTransport
from backend.tests.auth_helpers import sign_in
from backend.tests.databases import database_url
from backend.tests.test_admin_operations import seed
from backend.tests.test_harness_diagnostics import frame, usage
from backend.tests.test_harness_projection import observation, setup
from backend.tests.test_history_api import make_app
from backend.tests.test_runner_executor import (
    FakeDocker, FakeRunner, make_runner_app, receive_until_terminal, run_command,
)


@pytest.mark.parametrize("failure_type", [sqlite3.OperationalError, sqlite3.DatabaseError])
def test_raw_transport_failure_keeps_core_admin_detail(tmp_path, failure_type):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        run_id = seed(app, client)[0]

        def unavailable(_run_id):
            raise failure_type("private transport failure")

        app.state.runner_transport = SimpleNamespace(diagnostic_snapshots=unavailable)
        response = client.get(f"/api/admin/v1/runs/{run_id}")
        assert response.status_code == 200
        assert response.json()["run"]["id"] == run_id
        assert response.json()["harness_estimate"]["unavailable_reason"] == "projection_unavailable"
        assert "private transport failure" not in response.text


@pytest.mark.parametrize("failure_type", [sqlite3.OperationalError, sqlite3.DatabaseError])
def test_raw_transport_failure_does_not_hide_persisted_terminal(tmp_path, failure_type):
    app = make_runner_app(tmp_path, FakeDocker())
    with TestClient(app) as client:
        sign_in(client)

        def unavailable(_run_id):
            raise failure_type("private diagnostic failure")

        app.state.runner_transport.diagnostic_snapshots = unavailable
        runner = FakeRunner(app.state.runner_transport, app.state.runner_manager._client,
                            lambda command, container: [("run.started", {}), ("run.completed", {})])
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(run_command("synthetic"))
                events = receive_until_terminal(websocket)
            assert events[-1]["type"] == "run.completed"
            assert events[-1]["status"] == "completed"
        finally:
            runner.halt.set()
            runner.join(timeout=3)


@pytest.mark.asyncio
async def test_same_or_older_projection_does_not_take_write_lock(tmp_path):
    database = Database(database_url(tmp_path / "projection.db"))
    statements = []
    try:
        run_id = await setup(database)
        older, latest = observation(2), observation(3)
        await catch_up(database, run_id, [latest])

        def record(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.upper())

        event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        await catch_up(database, run_id, [latest])
        await catch_up(database, run_id, [older])
        changed_same_order = {**latest, "frame": {**latest["frame"],
                              "payload": {**latest["frame"]["payload"], "value_usd": "0.8"}}}
        with pytest.raises(ValueError, match="harness_observation_conflict"):
            await catch_up(database, run_id, [changed_same_order])
        assert not any("BEGIN IMMEDIATE" in sql or "FOR UPDATE" in sql or sql.startswith("UPDATE ")
                       or "LOCK_TIMEOUT" in sql for sql in statements)
    finally:
        await database.dispose()


def test_transport_snapshot_query_returns_only_latest_accepted_observation(tmp_path):
    transport = RunnerTransport(tmp_path / "transport.db")
    transport.message("p1", "run-1", "client", "synthetic", backend_run_id="backend-1")
    sources = []
    for index in range(1, 5):
        source = usage()
        source["event_id"] = str(uuid4())
        sources.append(source)
        transport.ingest("p1", index, source)
        if index != 4:  # An unreceived newest source must not hide an accepted receipt.
            transport.diagnostic("p1", frame(source, seq=index))
    observations = transport.diagnostic_snapshots("backend-1")
    assert len(observations) == 1
    assert observations[0]["frame"]["source_event_id"] == sources[2]["event_id"]


def test_existing_transport_bootstrap_adds_index_and_uses_bounded_plan(tmp_path):
    path = tmp_path / "transport.db"
    transport = RunnerTransport(path)
    transport.message("p1", "run-1", "client", "synthetic", backend_run_id="backend-1")
    # Simulate the pre-fix schema. Existing data survives repeated additive bootstrap.
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX diagnostic_source_order")
    transport = RunnerTransport(path)
    transport = RunnerTransport(path)
    with sqlite3.connect(path) as db:
        source = usage()
        metadata = frame(source, seq=1)
        import json
        sources = [(str(uuid4()), "p1", "run-1", "run-1", i, i) for i in range(1, 501)]
        db.executemany("INSERT INTO diagnostic_sources VALUES(?,?,?,?,?,?)", sources)
        receipts = []
        for source_id, *_ in sources:
            body = {**metadata, "source_event_id": source_id, "diagnostic_id": str(uuid4())}
            receipts.append((body["diagnostic_id"], "harness.diagnostic.v1", json.dumps(body), source_id, "now"))
        db.executemany("INSERT INTO diagnostic_receipts VALUES(?,?,?,?,?)", receipts)
        db.commit()
        assert transport.diagnostic_snapshots("backend-1")[0]["source_order"]["public_seq"] == 500
        sql = ("SELECT r.canonical_body,s.source_public_seq,s.source_journal_seq "
               "FROM diagnostic_sources s INDEXED BY diagnostic_source_order "
               "JOIN diagnostic_receipts r ON r.source_event_id=s.source_event_id "
               "WHERE s.command_id=? ORDER BY s.source_public_seq DESC LIMIT 1")
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, ("run-1",))]
        assert any("diagnostic_source_order" in step for step in plan)
        assert not any("TEMP B-TREE" in step for step in plan)
    assert len(transport.diagnostic_snapshots("backend-1")) == 1
