"""Isolated migration entrypoint used by release Jobs; never starts the web app."""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from backend.config import Settings


def migration_config(url: str) -> Config:
    config = Config(str(Path(__file__).with_name("alembic.ini")))
    config.attributes["database_url"] = url
    return config


def migration_head() -> str:
    script = ScriptDirectory.from_config(migration_config("sqlite+aiosqlite://"))
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError("Exactly one Alembic head is required")
    return heads[0]


async def inspect_database(
    url: str, *, require_head: bool = False, drift: bool = False
) -> dict:
    from backend import models  # noqa: F401
    from backend.db import Base

    script = ScriptDirectory.from_config(migration_config(url))
    head = migration_head()
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:

            def inspect(sync):
                current = MigrationContext.configure(sync).get_current_heads()
                if len(current) != 1:
                    raise RuntimeError("Expected one installed revision")
                # iterate_revisions also refuses an unknown source or a downgrade.
                list(script.iterate_revisions(head, current[0], implicit_base=False))
                if require_head and current != (head,):
                    raise RuntimeError("Database revision differs from image head")
                if drift and compare_metadata(
                    MigrationContext.configure(sync, opts={"compare_type": True}),
                    Base.metadata,
                ):
                    raise RuntimeError(
                        "Models differ from migrated schema; add an Alembic revision"
                    )
                return {"current": current[0], "head": head}

            return await connection.run_sync(inspect)
    finally:
        await engine.dispose()


async def record_release(url: str) -> bool:
    """Optional application audit hook; the controller knows no table or revision names."""
    from backend import models

    audit = getattr(models, "DeploymentAudit", None)
    if audit is None:
        return False
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    release = json.loads(os.environ["RELEASE_MANIFEST"])
    values = {
        "release_sha": release["sha"],
        "workflow_run_id": release["workflow_run_id"],
        "run_attempt": release["run_attempt"],
        "migration_head": migration_head(),
        "kind": "deployed",
        "backend_digest": release["images"]["backend"]["digest"],
        "frontend_digest": release["images"]["frontend"]["digest"],
    }
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session:
            exists = select(audit.id).where(
                audit.release_sha == values["release_sha"],
                audit.workflow_run_id == values["workflow_run_id"],
                audit.run_attempt == values["run_attempt"],
            )
            if await session.scalar(exists) is not None:
                return False
            session.add(audit(**values))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await session.scalar(exists) is None:
                    raise
            return True
    finally:
        await engine.dispose()


async def wait_for_database(url: str) -> None:
    """Connection-only retry before any DDL; kube readiness and routes settle separately."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    engine = create_async_engine(url, connect_args={"timeout": 3})
    deadline = time.monotonic() + 30
    try:
        while True:
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                return
            except (OSError, DBAPIError) as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Database TCP connection did not become ready"
                    ) from error
                await asyncio.sleep(1)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["plan", "upgrade", "verify", "check-models", "record"]
    )
    args = parser.parse_args()
    url = Settings().database_url
    asyncio.run(wait_for_database(url))
    if args.mode == "upgrade":
        # Same upgrade entrypoint in rehearsal and on the live DB.
        command.upgrade(migration_config(url), "head")
    result = asyncio.run(
        inspect_database(
            url, require_head=args.mode != "plan", drift=args.mode == "check-models"
        )
    )
    if args.mode == "record":
        result["recorded"] = asyncio.run(record_release(url))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
