"""Disposable, opt-in offline PG16.15 rehearsal. Never reads host DB settings."""

import asyncio
import os
import subprocess
import time

import pytest

from tools.cicd import maintenance_schema as module
from tools.cicd.schema_probe import (
    CatalogConnection,
    TargetMapping,
    canonical_signature,
    installed_catalog_profile,
    read_catalog,
)
from tools.cicd.tests.schema_pg_fixture import ROOT
from tools.cicd.tests.schema_pg_fixture import pg16 as pg16_fixture
from tools.cicd.tests.test_maintenance_schema import approval_for

pg16 = pg16_fixture


def migrate(pg, revision):
    # The classify job deliberately installs only workflow-test dependencies.
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    config.attributes["database_url"] = (
        f"postgresql+asyncpg://migrator@127.0.0.1:{pg.port}/probe_test"
    )
    command.upgrade(config, revision)


def inspect(pg):
    profile = installed_catalog_profile()
    catalog = asyncio.run(
        read_catalog(
            CatalogConnection("127.0.0.1", pg.port, "probe_test", "probe", profile)
        )
    )
    # Full rows, no LIMIT/filter. Fixture statement independent of catalog read;
    # database is fully quiescent and owned by this single test.
    rows = pg.execute(
        "SELECT version_num FROM public.alembic_version ORDER BY version_num"
    )
    return module.LiveSchema(catalog, tuple(rows.splitlines()))


def mapping_for(pg):
    return TargetMapping(
        "127.0.0.1",
        pg.port,
        "/fixture/not-used-ca.pem",
        "owned-disposable-pg16",
        "probe_test",
        "probe",
        ("public",),
        installed_catalog_profile(),
    )


def psql_for(pg):
    # No shell, network/pull, production credentials, or host PG* environment.
    # DDL uses the existing dedicated migrator role, NOT postgres/superuser.
    argv = [
        "docker",
        "exec",
        "-i",
        pg.container,
        "psql",
        "-X",
        "--no-password",
        "--set=ON_ERROR_STOP=1",
        "--quiet",
        "-U",
        "migrator",
        "-d",
        "probe_test",
    ]

    def spawn():
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"},
        )

    return module.BoundedPsql(spawn)


def test_exact_sql_matches_alembic_target_and_preserves_data(pg16):
    migrate(pg16, module.SOURCE_REVISION)
    source = inspect(pg16)
    # Produce independent target with the actual reviewed migration on this owned
    # cluster, then return only this disposable fixture to old schema for rehearsal.
    migrate(pg16, module.TARGET_REVISION)
    expected = inspect(pg16)
    # Recreate only this disposable fixture; no operational downgrade exists.
    pg16.execute(
        "DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION migrator"
    )
    migrate(pg16, module.SOURCE_REVISION)
    assert canonical_signature(
        inspect(pg16).catalog, mapping_for(pg16).profile
    ) == canonical_signature(source.catalog, mapping_for(pg16).profile)
    pg16.execute(
        "INSERT INTO public.users (id,email,password_hash,role,created_at) "
        "VALUES ('fixture-user','fixture@example.invalid','not-a-secret','user',CURRENT_TIMESTAMP)"
    )
    approval = approval_for(source.catalog, expected.catalog, mapping_for(pg16))
    proof = module.MigrationBoundary(
        approval,
        inspect=lambda: inspect(pg16),
        psql=psql_for(pg16),
    ).apply_once()
    assert proof.signature == canonical_signature(
        expected.catalog, approval.mapping.profile
    )
    assert proof.revision == module.TARGET_REVISION
    assert (
        pg16.execute(
            "SELECT id || ':' || ai_frozen::text || ':' || ai_policy_version::text "
            "FROM public.users WHERE id='fixture-user'"
        )
        == "fixture-user:false:0"
    )
    assert pg16.execute("SELECT count(*) FROM public.ai_policy_audits") == "0"
    assert (
        pg16.execute("SELECT version_num FROM public.alembic_version")
        == module.TARGET_REVISION
    )


@pytest.mark.parametrize("revision", ["wrong", "duplicate", "empty"])
def test_transaction_rejects_exact_old_revision_guard(pg16, revision):
    migrate(pg16, module.SOURCE_REVISION)
    if revision == "wrong":
        pg16.execute("UPDATE public.alembic_version SET version_num='unexpected'")
    elif revision == "duplicate":
        pg16.execute("INSERT INTO public.alembic_version VALUES ('other')")
    else:
        pg16.execute("DELETE FROM public.alembic_version")
    with pytest.raises(module.MigrationRefused):
        psql_for(pg16).run(module.PROCEDURE, timeout_seconds=60)
    assert (
        pg16.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' AND column_name='ai_frozen'"
        )
        == "0"
    )


def test_transaction_lock_deadline_refuses_without_applying(pg16):
    migrate(pg16, module.SOURCE_REVISION)

    async def hold_and_apply():
        connection = await pg16.connect("migrator")
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute("LOCK TABLE public.users IN ACCESS EXCLUSIVE MODE")
            started = time.monotonic()
            with pytest.raises(module.MigrationRefused):
                await asyncio.to_thread(
                    psql_for(pg16).run, module.PROCEDURE, timeout_seconds=60
                )
            assert time.monotonic() - started < 15
        finally:
            await transaction.rollback()
            await connection.close(timeout=2)

    asyncio.run(hold_and_apply())
    assert (
        pg16.execute("SELECT version_num FROM public.alembic_version")
        == module.SOURCE_REVISION
    )
    assert (
        pg16.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' AND column_name='ai_frozen'"
        )
        == "0"
    )


def test_late_ddl_error_rolls_back_prior_changes_and_revision(pg16):
    migrate(pg16, module.SOURCE_REVISION)
    # Fault injection on the disposable DB, never on procedure bytes.
    pg16.execute("CREATE TABLE public.ai_policy_audits (id text)")
    with pytest.raises(module.MigrationRefused):
        psql_for(pg16).run(module.PROCEDURE, timeout_seconds=60)
    assert (
        pg16.execute("SELECT version_num FROM public.alembic_version")
        == module.SOURCE_REVISION
    )
    assert (
        pg16.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' AND column_name='ai_frozen'"
        )
        == "0"
    )
    assert (
        pg16.execute(
            "SELECT count(*) FROM pg_indexes WHERE indexname='ix_runs_started_id'"
        )
        == "0"
    )
