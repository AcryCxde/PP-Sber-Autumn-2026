"""Explicit offline bridge for the deployed migration fork; never called by startup.

Stop ALL writers, verify a restorable backup, and use a direct database connection.
Preflight is read-only by default. Apply requires --apply --backup-verified.
On failure restore the backup before retrying; there is no automatic downgrade.
The original revision and verified fork are printed as migration history evidence.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine

HEAD = "20261111_0004"
VERSIONS = Path(__file__).with_name("migrations") / "versions"


def _revision(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(connection, paths: list[Path]) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        for path in paths:
            _revision(path).upgrade()


def _paths(fork: str, revision: str) -> list[Path]:
    paths = sorted(VERSIONS.glob("*.py"))
    if fork == "master":
        return [p for p in paths if p.name[:13] <= revision]
    stage = int(revision[-4:])
    return [p for p in paths if p.name[:13] <= "20261029_0010"] + [
        p for p in paths if p.name.startswith("20261111_") and int(p.name[9:13]) <= stage - 10
    ]


def _signature(connection) -> dict:
    inspector = sa.inspect(connection)
    signature = {}
    for table in sorted(set(inspector.get_table_names()) - {"alembic_version"}):
        columns = []
        primary_columns = inspector.get_pk_constraint(table)["constrained_columns"]
        for column in inspector.get_columns(table):
            datatype = column["type"]
            default = column.get("default")
            if default and default.endswith("::character varying"):
                default = default.removesuffix("::character varying")
            if isinstance(datatype, sa.Boolean) and default in {"false", "0", "(0)"}:
                default = "false"
            elif default in {"0", "'0'"}:
                default = "0"
            if (column["name"] in primary_columns and isinstance(datatype, sa.Integer)
                    and default == f"nextval('{table}_{column['name']}_seq'::regclass)"):
                default = None
            columns.append((column["name"], datatype._type_affinity.__name__,
                            getattr(datatype, "length", None), column["nullable"], default))
        foreign = sorted((tuple(f["constrained_columns"]), f["referred_table"],
                          tuple(f["referred_columns"]), f.get("options", {}).get("ondelete"))
                         for f in inspector.get_foreign_keys(table))
        unique = sorted(tuple(u["column_names"]) for u in inspector.get_unique_constraints(table))
        indexes = sorted((i["name"], tuple(i["column_names"]), bool(i["unique"]))
                         for i in inspector.get_indexes(table) if not i.get("duplicates_constraint"))
        pg_status_check = "status::text = ANY (ARRAY['pending'::character varying, 'confirming'::character varying, 'confirmed'::character varying, 'conflicted'::character varying]::text[])"
        checks = sorted(
            "status IN ('pending', 'confirming', 'confirmed', 'conflicted')"
            if c["sqltext"] == pg_status_check else " ".join(c["sqltext"].split())
            for c in inspector.get_check_constraints(table)
        )
        signature[table] = (sorted(columns), tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
                            foreign, unique, indexes, checks)
    return signature


def validate_schema(connection, fork: str, revision: str) -> None:
    # Reconstruct the actual historical DDL, not current ORM assumptions. No source
    # database mutation occurs while determining the signature.
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as reference:
            _apply(reference, _paths(fork, revision))
            expected = _signature(reference)
        actual = _signature(connection)
        if actual != expected:
            differences = sorted(k for k in actual.keys() | expected.keys() if actual.get(k) != expected.get(k))
            raise RuntimeError("Migration schema mismatch; refusing DDL: " + ", ".join(differences))
    finally:
        engine.dispose()


def preflight_canonical(connection) -> None:
    if "alembic_version" not in sa.inspect(connection).get_table_names():
        return
    revisions = list(connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars())
    if len(revisions) != 1:
        raise RuntimeError("Expected one migration head; refusing automatic upgrade")
    revision = revisions[0]
    if revision in {"20261030_0011", "20261030_0012"}:
        validate_schema(connection, "master", revision)
    elif revision == "20261030_0013":
        raise RuntimeError("Deployed migration fork requires explicit backend.migration_bridge maintenance preflight")


def bridge(connection, *, apply: bool = False, backup_verified: bool = False) -> dict[str, str]:
    revisions = list(connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars())
    if revisions == [HEAD]:
        raise RuntimeError("Already migrated; refusing bridge rerun")
    if revisions != ["20261030_0013"]:
        raise RuntimeError("Bridge requires deployed 20261030_0013; earlier or unknown revisions are not supported")
    validate_schema(connection, "deployed", revisions[0])
    evidence = {"source_fork": "deployed-product-storage-activity", "source_revision": revisions[0],
                "target_revision": HEAD, "operation": "preflight"}
    if not apply:
        return evidence
    if not backup_verified:
        raise RuntimeError("Apply requires a verified backup and stopped writers")
    _apply(connection, sorted(VERSIONS.glob("20261030_*.py")))
    _apply(connection, [VERSIONS / "20261111_0004_answer_fingerprint.py"])
    validate_schema(connection, "master", HEAD)
    result = connection.execute(sa.text("UPDATE alembic_version SET version_num=:target WHERE version_num=:source"),
                                {"target": HEAD, "source": revisions[0]})
    if result.rowcount != 1:
        raise RuntimeError("Migration head changed during bridge")
    return {**evidence, "operation": "applied"}


async def _run(args) -> None:
    # Read from environment, never print connection credentials.
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            if connection.dialect.name == "sqlite":
                await connection.exec_driver_sql("BEGIN IMMEDIATE" if args.apply else "BEGIN")
            evidence = await connection.run_sync(
                lambda conn: bridge(conn, apply=args.apply, backup_verified=args.backup_verified))
        print(evidence)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=["deployed-20261030-0013"])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-verified", action="store_true")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
