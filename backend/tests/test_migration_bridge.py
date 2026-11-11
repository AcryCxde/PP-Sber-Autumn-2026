"""Historical schema fixtures, fail-closed preflight, and transactional bridge."""
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from backend.migration_bridge import (
    HEAD, VERSIONS, _apply, _paths, bridge, preflight_canonical, validate_schema,
)


def historical(connection, fork, revision):
    _apply(connection, _paths(fork, revision))
    connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
    connection.execute(sa.text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": revision})


@pytest.mark.parametrize("fork", ["master", "deployed"])
@pytest.mark.parametrize("revision", ["20261030_0011", "20261030_0012"])
def test_colliding_ids_require_structural_evidence(fork, revision):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        historical(connection, fork, revision)
        if fork == "master":
            preflight_canonical(connection)
        else:
            with pytest.raises(RuntimeError, match="schema mismatch"):
                preflight_canonical(connection)
        connection.execute(sa.text("ALTER TABLE users ADD COLUMN unexpected TEXT"))
        with pytest.raises(RuntimeError, match="schema mismatch"):
            preflight_canonical(connection)
    engine.dispose()


def test_bridge_preflight_apply_and_replay_refusal():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        historical(connection, "deployed", "20261030_0013")
        assert bridge(connection)["operation"] == "preflight"
        assert "artifact_confirmations" not in sa.inspect(connection).get_table_names()
        with pytest.raises(RuntimeError, match="backup"):
            bridge(connection, apply=True)
        assert bridge(connection, apply=True, backup_verified=True)["target_revision"] == HEAD
        with pytest.raises(RuntimeError, match="Already migrated"):
            bridge(connection)
    engine.dispose()


def test_bridge_rejects_partial_schema_before_ddl():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        historical(connection, "deployed", "20261030_0013")
        connection.execute(sa.text("ALTER TABLE artifacts ADD COLUMN unexpected TEXT"))
        with pytest.raises(RuntimeError, match="schema mismatch"):
            bridge(connection, apply=True, backup_verified=True)
        assert "artifact_confirmations" not in sa.inspect(connection).get_table_names()
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20261030_0013"
    engine.dispose()


@pytest.mark.skipif(not os.environ.get("PPS_TEST_POSTGRES_URL"), reason="Disposable PostgreSQL URL not supplied")
@pytest.mark.parametrize("fork", ["fresh", "master", "deployed", "rollback"])
async def test_postgres_migration_histories(fork):
    engine = create_async_engine(os.environ["PPS_TEST_POSTGRES_URL"])
    schema = "test_bridge_" + fork + "_" + uuid4().hex[:8]
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            if fork == "fresh":
                await connection.run_sync(lambda c: _apply(c, _paths("master", HEAD)))
            elif fork == "master":
                await connection.run_sync(lambda c: historical(c, "master", "20261030_0012"))
                await connection.run_sync(preflight_canonical)
                await connection.run_sync(lambda c: _apply(c, sorted(VERSIONS.glob("20261111_*.py"))))
            else:
                await connection.run_sync(lambda c: historical(c, "deployed", "20261030_0013"))
            await connection.run_sync(lambda c: validate_schema(c, "deployed" if fork in {"deployed", "rollback"} else "master", "20261030_0013" if fork in {"deployed", "rollback"} else HEAD))
            if fork in {"deployed", "rollback"}:
                await connection.execute(sa.text("INSERT INTO users (id,email,password_hash,role,created_at) VALUES ('marker','marker@example.test','hash','user',CURRENT_TIMESTAMP)"))
        if fork == "rollback":
            with pytest.raises(RuntimeError, match="injected"):
                async with engine.begin() as connection:
                    await connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
                    await connection.run_sync(lambda c: bridge(c, apply=True, backup_verified=True))
                    raise RuntimeError("injected failure after transactional DDL")
            async with engine.begin() as connection:
                await connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
                await connection.run_sync(lambda c: validate_schema(c, "deployed", "20261030_0013"))
                assert await connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20261030_0013"
        elif fork == "deployed":
            async with engine.begin() as connection:
                await connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
                await connection.run_sync(lambda c: bridge(c, apply=True, backup_verified=True))
                assert await connection.scalar(sa.text("SELECT email FROM users WHERE id='marker'")) == "marker@example.test"
                assert await connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD
    finally:
        await engine.dispose()
