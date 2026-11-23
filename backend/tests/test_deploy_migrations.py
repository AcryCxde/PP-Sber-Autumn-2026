"""Deployment startup must verify the image head without running any DDL."""

import asyncio

import pytest
from alembic import command
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.db import Database, upgrade_database
from backend.main import create_app
from backend.migrate import inspect_database, migration_config, record_release


def test_deployment_startup_verifies_without_migrating(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'database' / 'deployed.db'}"
    upgrade_database(url)
    database = Database(url)

    async def forbidden():
        raise AssertionError("Web startup attempted DDL")

    monkeypatch.setattr(database, "migrate", forbidden)
    settings = Settings(
        database_url=url,
        database_migrations="verify",
        workspace_root=tmp_path / "workspace",
    )
    with TestClient(create_app(database=database, settings=settings)) as client:
        assert client.get("/api/auth/me").status_code == 401


def test_deployment_refuses_old_schema_before_web_writes(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    command.upgrade(migration_config(url), "20261111_0005")
    with pytest.raises(RuntimeError, match="revision differs"):
        asyncio.run(inspect_database(url, require_head=True))
    assert asyncio.run(inspect_database(url))["current"] == "20261111_0005"


def test_optional_release_audit_hook_before_history_model_exists(monkeypatch):
    from backend import models

    monkeypatch.delattr(models, "DeploymentAudit", raising=False)
    assert asyncio.run(record_release("sqlite+aiosqlite://")) is False


def test_job_retries_connection_before_any_migration(monkeypatch):
    from backend import migrate

    attempts = []

    class Connection:
        async def __aenter__(self):
            attempts.append("connect")
            if len(attempts) < 3:
                raise ConnectionRefusedError("transient pod route")
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement):
            assert str(statement) == "SELECT 1"

    class Engine:
        def connect(self):
            return Connection()

        async def dispose(self):
            attempts.append("disposed")

    async def sleep(seconds):
        assert seconds == 1

    monkeypatch.setattr(migrate, "create_async_engine", lambda *a, **kw: Engine())
    monkeypatch.setattr(migrate.asyncio, "sleep", sleep)
    asyncio.run(migrate.wait_for_database("postgresql+asyncpg://unused"))
    assert attempts == ["connect", "connect", "connect", "disposed"]
