"""Restore synthetic historical data, then bridge/upgrade on owned PG16.15 only."""

import asyncio
import subprocess
import sys

import pytest

from tools.cicd.schema_baseline import ENV, ROOT, _psql, migrate_release, release_source
from tools.cicd.schema_probe import (
    CatalogConnection,
    canonical_signature,
    installed_catalog_profile,
    read_catalog,
)
from tools.cicd.tests.schema_pg_fixture import pg16 as pg16_fixture
from tools.cicd.tests.test_schema_baseline import SHA
from tools.cicd.tests.test_schema_release_compatibility import CANDIDATE_SHA

pg16 = pg16_fixture

# Trusted test harness outside archived source; never imports application config.
RESTORED_RUNNER = """
import asyncio, importlib.util, pathlib, sys
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from alembic import command
from alembic.config import Config
source, authority, port, database, operation = sys.argv[1:]
spec = importlib.util.spec_from_file_location('bridge', pathlib.Path(source)/'backend/migration_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
config = Config()
config.set_main_option('script_location', authority)
config.set_main_option('version_locations', str(pathlib.Path(source)/'backend/migrations/versions'))
async def run():
    engine = create_async_engine('postgresql+asyncpg://migrator@127.0.0.1:'+port+'/'+database,
        connect_args={'timeout':5, 'server_settings':{'lock_timeout':'1000','statement_timeout':'5000'}})
    try:
        async with engine.begin() as connection:
            def execute(conn):
                config.attributes['connection'] = conn
                if operation == 'seed_fork':
                    bridge._apply(conn, bridge._paths('deployed', '20261030_0013'))
                    command.ensure_version(config)
                    conn.execute(sa.text("INSERT INTO alembic_version VALUES ('20261030_0013')"))
                    return
                revision = conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one()
                if revision == '20261030_0013':
                    result = bridge.bridge(conn, apply=True, backup_verified=True)
                    assert result['target_revision'] == '20261111_0005'
                    assert conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one() == '20261111_0005'
                else:
                    assert revision == '20261111_0005'
                command.upgrade(config, '20261112_0001')
                assert conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one() == '20261112_0001'
            await connection.run_sync(execute)
    finally:
        await engine.dispose()
asyncio.run(run())
"""


def restored_runner(source, authority, pg16, database, operation):
    (authority / "env.py").write_text(ENV)
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            RESTORED_RUNNER,
            str(source),
            str(authority),
            str(pg16.port),
            database,
            operation,
        ],
        cwd=authority,
        check=True,
        capture_output=True,
        timeout=60,
    )


@pytest.mark.parametrize("historical", ["canonical_0005", "deployed_0013"])
def test_restored_user_and_run_invariants_converge(pg16, tmp_path, historical):
    databases = ("baseline_original", "baseline_restored", "baseline_clean")
    profile = installed_catalog_profile()
    try:
        for database in databases:
            _psql(pg16.container, f"CREATE DATABASE {database} OWNER migrator")
            _psql(
                pg16.container,
                f"REVOKE CREATE, TEMP ON DATABASE {database} FROM PUBLIC; "
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC; "
                "GRANT CREATE ON SCHEMA public TO migrator",
                database,
            )
        with (
            release_source(ROOT, SHA) as legacy,
            release_source(ROOT, CANDIDATE_SHA) as candidate,
        ):
            if historical == "canonical_0005":
                migrate_release(legacy, pg16.port, databases[0], "clean")
            else:
                restored_runner(candidate, tmp_path, pg16, databases[0], "seed_fork")
            _psql(
                pg16.container,
                """
                INSERT INTO users (id,email,password_hash,created_at)
                VALUES ('owner','owner@example.test','synthetic-hash','2026-11-11');
                INSERT INTO conversations (id,owner_id,title,status,context_state,next_message_seq,created_at,updated_at)
                VALUES ('conversation','owner','Synthetic history','active','new',0,'2026-11-11','2026-11-11');
                INSERT INTO runs (id,conversation_id,status,client_request_id,context_mode,next_event_seq)
                VALUES ('historical','conversation','completed','request','new',0);
            """,
                databases[0],
            )
            dump = subprocess.run(
                [
                    "docker",
                    "exec",
                    pg16.container,
                    "pg_dump",
                    "-U",
                    "postgres",
                    "--data-only",
                    "--no-owner",
                    "--no-acl",
                    databases[0],
                ],
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
            # Reconstruct vetted historical DDL, then restore synthetic rows. A
            # pg_dump schema roundtrip rewrites an existing partial-index AST;
            # the strict catalog intentionally does not normalize that difference.
            if historical == "canonical_0005":
                migrate_release(legacy, pg16.port, databases[1], "clean")
            else:
                restored_runner(candidate, tmp_path, pg16, databases[1], "seed_fork")
            _psql(pg16.container, "DELETE FROM alembic_version", databases[1])
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    pg16.container,
                    "psql",
                    "-X",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-U",
                    "migrator",
                    "-d",
                    databases[1],
                ],
                input=dump,
                check=True,
                capture_output=True,
                timeout=30,
            )
            restored_runner(candidate, tmp_path, pg16, databases[1], "upgrade")
            assert (
                _psql(
                    pg16.container,
                    "SELECT id,email,password_hash,ai_frozen,ai_policy_version FROM users",
                    databases[1],
                )
                == "owner|owner@example.test|synthetic-hash|f|0"
            )
            assert (
                _psql(
                    pg16.container,
                    "SELECT id,status,started_at IS NULL,cost_usd IS NULL,admitted_at IS NULL,admission_policy_version IS NULL,harness_estimate IS NULL FROM runs",
                    databases[1],
                )
                == "historical|completed|t|t|t|t|t"
            )
            if (
                _psql(
                    pg16.container,
                    "SELECT count(*) FROM information_schema.columns WHERE table_name='runs' AND column_name='admission_attempt_id'",
                    databases[1],
                )
                == "1"
            ):
                assert (
                    _psql(
                        pg16.container,
                        "SELECT admission_attempt_id IS NULL FROM runs",
                        databases[1],
                    )
                    == "t"
                )
            assert (
                _psql(
                    pg16.container,
                    "SELECT count(*) FROM ai_policy_audits",
                    databases[1],
                )
                == "0"
            )
            migrate_release(candidate, pg16.port, databases[2], "clean")
            signatures = []
            snapshots = []
            for database in databases[1:]:
                snapshot = asyncio.run(
                    read_catalog(
                        CatalogConnection(
                            "127.0.0.1", pg16.port, database, "probe", profile
                        )
                    )
                )
                snapshots.append(snapshot)
                signatures.append(canonical_signature(snapshot, profile))
            assert (
                snapshots[0]["database_collation"] == snapshots[1]["database_collation"]
            )
            import json

            for section in snapshots[0]["inventory"]:
                # Catalog arrays are unordered except columns. Canonicalizer
                # sorts dependency edges by both endpoints AND dependency type.
                left, right = (snapshot["inventory"][section] for snapshot in snapshots)
                if section != "columns":
                    left = sorted(json.dumps(row, sort_keys=True) for row in left)
                    right = sorted(json.dumps(row, sort_keys=True) for row in right)
                assert left == right, section
            assert signatures[0] == signatures[1]
    finally:
        for database in reversed(databases):
            _psql(pg16.container, f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
