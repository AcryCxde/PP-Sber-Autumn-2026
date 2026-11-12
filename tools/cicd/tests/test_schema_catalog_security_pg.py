"""Fail-closed derived-object exceptions on an owned, opt-in PostgreSQL 16.15."""

import asyncio

import pytest

from tools.cicd.schema_probe import (
    CatalogConnection,
    ProbeError,
    canonical_signature,
    installed_catalog_profile,
    read_catalog,
)
from tools.cicd.tests.schema_pg_fixture import pg16 as pg16_fixture

pg16 = pg16_fixture


def signature(pg):
    profile = installed_catalog_profile()
    mapping = CatalogConnection("127.0.0.1", pg.port, "probe_test", "probe", profile)
    return canonical_signature(asyncio.run(read_catalog(mapping)), profile)


@pytest.mark.parametrize(
    "ddl",
    [
        "ALTER EXTENSION plpgsql ADD TABLE sample",
        "ALTER EXTENSION plpgsql DROP FUNCTION plpgsql_validator(oid)",
        "ALTER FUNCTION plpgsql_validator(oid) IMMUTABLE",
        "DROP EXTENSION plpgsql",
        "CREATE TABLE consumer(value sample)",
        "CREATE TABLE consumer(value sample[])",
    ],
)
def test_derived_exception_does_not_authorize_user_objects(pg16, ddl):
    pg16.execute("CREATE TABLE sample(id integer)")
    assert signature(pg16)
    pg16.execute(ddl)
    with pytest.raises(ProbeError):
        signature(pg16)


@pytest.mark.parametrize(
    "action", ["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"]
)
def test_reviewed_derived_trigger_profiles_remain_supported(pg16, action):
    pg16.execute(
        "CREATE TABLE parent(id integer PRIMARY KEY); "
        "CREATE TABLE sample(id integer, value integer, "
        "UNIQUE(value) DEFERRABLE INITIALLY DEFERRED, "
        f"FOREIGN KEY(id) REFERENCES parent(id) ON UPDATE {action} "
        f"ON DELETE {action} DEFERRABLE INITIALLY DEFERRED)"
    )
    assert signature(pg16)


@pytest.mark.parametrize(
    "change",
    [
        "tgtype=21",
        "tgfoid='pg_catalog.unique_key_recheck()'::regprocedure",
        "tgdeferrable=true",
        "tgconstrrelid=0",
        "tgconstrindid=0",
    ],
)
def test_fk_trigger_event_tamper_rejected(pg16, change):
    pg16.execute(
        "CREATE TABLE parent(id integer PRIMARY KEY); "
        "CREATE TABLE sample(id integer REFERENCES parent(id))"
    )
    assert signature(pg16)
    # Catalog injection belongs solely to this fixture's throwaway database.
    pg16.execute(
        f"UPDATE pg_trigger SET {change} "
        "WHERE tgrelid='sample'::regclass AND tgisinternal"
    )
    with pytest.raises(ProbeError):
        signature(pg16)
