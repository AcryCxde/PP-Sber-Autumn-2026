"""Operational admin reads use run cohorts, never event counts or raw content."""
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.models import Conversation, Run, User
from backend.tests.auth_helpers import sign_in
from backend.tests.test_history_api import make_app


def seed(app, client):
    async def scenario():
        async with app.state.database.session() as session:
            user = await session.scalar(select(User))
            user.role = "admin"
            start = datetime(2026, 11, 1, tzinfo=UTC)
            ids = []
            for index, (status, stamp) in enumerate([
                ("failed", start - timedelta(microseconds=1)), ("failed", start),
                ("outcome_unknown", start + timedelta(hours=1)),
                ("running", start + timedelta(hours=2)),
                ("completed", start + timedelta(days=1)), ("pending", None),
                ("running", start - timedelta(days=2)),
            ]):
                conversation = Conversation(owner_id=user.id if index != 2 else None, title="SECRET_PROMPT")
                session.add(conversation)
                await session.flush()
                run = Run(conversation_id=conversation.id, status=status,
                    client_request_id=str(uuid4()), started_at=stamp,
                    completed_at=stamp if status in {"failed", "outcome_unknown", "completed"} else None,
                    error_code="SECRET_ERROR", error="SECRET_ERROR_DETAILS")
                session.add(run)
                await session.flush()
                ids.append(run.id)
            await session.commit()
            return ids
    return client.portal.call(scenario)


def test_overview_and_drilldown_share_half_open_cohort(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        ids = seed(app, client)
        params = {"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z", "attention_start": "2026-11-01T00:00:00Z", "attention_end": "2026-11-02T00:00:00Z", "page_size": 2}
        response = client.get("/api/admin/v1/overview", params=params)
        assert response.status_code == 200
        body = response.json()
        assert body["totals"] == {"runs": 3, "failed": 1, "outcome_unknown": 1, "running": 1}
        assert body["active"]["total"] == 3
        assert body["without_started_at"] == 1
        assert body["attention"]["total"] == 2
        assert len(body["active"]["items"]) == 2
        assert response.headers["cache-control"] == "no-store"
        drilldown = client.get("/api/admin/v1/runs", params={**params, "status": "outcome_unknown"}).json()
        assert drilldown["total"] == 1
        assert drilldown["items"][0]["id"] == ids[2]
        assert drilldown["items"][0]["user"] is None
        detail = client.get(f"/api/admin/v1/runs/{ids[1]}").json()
        assert detail["harness_estimate"]["state"] == "unknown"
        assert detail["run"]["error_code"] == "unclassified"
        assert "SECRET" not in response.text + str(detail)


def test_operational_access_and_invalid_period(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/admin/v1/overview").status_code == 401
        sign_in(client)
        assert client.get("/api/admin/v1/overview").status_code == 403
        seed(app, client)
        for params in [
            {"start": "2026-11-01T00:00:00", "end": "2026-11-02T00:00:00Z"},
            {"start": "2026-11-02T00:00:00Z", "end": "2026-11-01T00:00:00Z"},
            {"page_size": 0},
        ]:
            assert client.get("/api/admin/v1/overview", params=params).status_code == 422
        assert client.get("/api/admin/v1/runs/missing").status_code == 404


def test_detail_uses_only_scoped_snapshot_not_scalar_or_sum(tmp_path):
    from backend.harness_estimate import project_snapshot
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        ids = seed(app, client)
        async def save():
            async with app.state.database.session() as session:
                run = await session.get(Run, ids[0])
                run.cost_usd = 999
                base = {"session_ref": str(uuid4()), "journal_seq": 2,
                        "observed_at": "2026-11-12T00:00:00Z", "value_usd": "0.015435"}
                first = project_snapshot(base, event_id=str(uuid4()), previous=None)
                latest = project_snapshot({**base, "journal_seq": 3, "value_usd": "0.062745"},
                                          event_id=str(uuid4()), previous=first)
                assert project_snapshot(base, event_id=str(uuid4()), previous=latest) == latest
                run.harness_estimate = latest
                await session.commit()
        client.portal.call(save)
        body = client.get(f"/api/admin/v1/runs/{ids[0]}").json()
        assert body["harness_estimate"]["value_usd"] == "0.062745"
        assert body["harness_estimate"]["scope"] == "session_total"
        assert body["harness_estimate"]["baseline"] is None
        assert body["harness_estimate"]["continuity"] == "unverified"


def test_no_start_attention_is_separate_and_detail_is_safe(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        ids = seed(app, client)
        async def fail():
            async with app.state.database.session() as session:
                run = await session.get(Run, ids[5])
                run.status = "failed"
                await session.commit()
        client.portal.call(fail)
        body = client.get("/api/admin/v1/overview", params={"attention_no_start": "true"}).json()
        assert body["attention"]["total"] == 1
        assert body["attention"]["items"][0]["id"] == ids[5]
        assert body["attention"]["items"][0]["started_at"] is None


def test_stage_proof_and_mismatch_never_use_current_project_stage(tmp_path):
    from types import SimpleNamespace
    from backend.admin_queries import _stage
    base = {"stage": 0, "product_run_id": None, "scenario": "other", "product": None, "stage_id": None}
    assert _stage(SimpleNamespace(**base))["source"] == "scenario_catalog"
    assert _stage(SimpleNamespace(**{**base, "scenario": "unknown"})) is None
    assert _stage(SimpleNamespace(**{**base, "stage": -1})) is None
    product = {**base, "product_run_id": "p", "stage_id": "s",
               "product": {"stages": [{"id": "s", "title": "Stage"}]}}
    assert _stage(SimpleNamespace(**product))["source"] == "product_stage"
    assert _stage(SimpleNamespace(**{**product, "stage_id": "mismatch"})) is None


def test_overview_is_one_sql_snapshot_with_bounded_pages(tmp_path):
    from sqlalchemy import event
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        seed(app, client)
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            if "counts AS" in statement and "selected AS" in statement:
                statements.append(statement)
        event.listen(app.state.database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            response = client.get("/api/admin/v1/overview", params={"page_size": 1})
            assert response.status_code == 200
        finally:
            event.remove(app.state.database.engine.sync_engine, "before_cursor_execute", capture)
        assert len(statements) == 1
        assert statements[0].upper().count("LIMIT") >= 2


def test_empty_overview_has_known_zero_and_no_false_rows(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        async def grant():
            async with app.state.database.session() as session:
                user = await session.scalar(select(User))
                user.role = "admin"
                await session.commit()
        client.portal.call(grant)
        body = client.get("/api/admin/v1/overview").json()
        assert body["totals"] == {"runs": 0, "failed": 0, "outcome_unknown": 0, "running": 0}
        assert body["active"]["items"] == []
        assert client.get("/api/admin/v1/runs").json()["items"] == []


def test_synthetic_large_cohort_explain_keeps_pages_bounded(tmp_path):
    from sqlalchemy import event
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        ids = seed(app, client)
        async def populate():
            async with app.state.database.session() as session:
                original = await session.get(Run, ids[0])
                for _ in range(500):
                    session.add(Run(conversation_id=original.conversation_id, status="completed",
                        client_request_id=str(uuid4()), started_at=datetime(2026, 11, 1, tzinfo=UTC)))
                await session.commit()
        client.portal.call(populate)
        captured = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            if "counts AS" in statement and "selected AS" in statement:
                captured.append((statement, parameters))
        event.listen(app.state.database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            body = client.get("/api/admin/v1/overview", params={"page_size": 2}).json()
        finally:
            event.remove(app.state.database.engine.sync_engine, "before_cursor_execute", capture)
        assert len(body["active"]["items"]) <= 2
        async def explain():
            statement, params = captured[0]
            prefix = "EXPLAIN " if app.state.database.engine.dialect.name == "postgresql" else "EXPLAIN QUERY PLAN "
            async with app.state.database.engine.connect() as connection:
                result = await connection.exec_driver_sql(prefix + statement, params)
                return str(result.all())
        explanation = client.portal.call(explain)
        assert explanation
        if app.state.database.engine.dialect.name == "postgresql":
            assert "Limit" in explanation
        else:
            assert "ix_runs" in explanation


def test_postgres_overview_keeps_statement_snapshot_during_status_update(tmp_path):
    import threading
    import pytest
    from sqlalchemy import event
    from backend.db import Database
    app = make_app(tmp_path)
    if app.state.database.engine.dialect.name != "postgresql":
        pytest.skip("concurrent statement snapshot requires PostgreSQL")
    with TestClient(app) as client:
        sign_in(client)
        ids = seed(app, client)
        completed = []
        errors = []
        def update_after_statement(conn, cursor, statement, parameters, context, executemany):
            if "counts AS" not in statement or completed:
                return
            completed.append(True)
            async def update():
                db = Database(app.state.database.url)
                try:
                    async with db.session() as session:
                        run = await session.get(Run, ids[3])
                        run.status = "failed"
                        await session.commit()
                finally:
                    await db.dispose()
            def worker():
                try:
                    asyncio.run(update())
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=10)
            assert not thread.is_alive()
        event.listen(app.state.database.engine.sync_engine, "after_cursor_execute", update_after_statement)
        try:
            body = client.get("/api/admin/v1/overview", params={"start": "2026-11-01T00:00:00Z",
                "end": "2026-11-02T00:00:00Z"}).json()
        finally:
            event.remove(app.state.database.engine.sync_engine, "after_cursor_execute", update_after_statement)
        assert not errors
        assert completed
        assert body["totals"]["running"] == 1
        assert any(row["id"] == ids[3] and row["status"] == "running" for row in body["active"]["items"])
        assert client.get(f"/api/admin/v1/runs/{ids[3]}").json()["run"]["status"] == "failed"
