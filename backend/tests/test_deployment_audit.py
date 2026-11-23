import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from backend.db import upgrade_database
from backend.migrate import inspect_database, record_release
from backend.models import DeploymentAudit


def test_new_history_model_and_data_are_migrated_and_recording_is_idempotent(
    tmp_path, monkeypatch
):
    url = f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}"
    upgrade_database(url)
    manifest = {
        "sha": "a" * 40,
        "workflow_run_id": 45000000001,
        "run_attempt": 1,
        "images": {
            "backend": {"digest": "sha256:" + "b" * 64},
            "frontend": {"digest": "sha256:" + "c" * 64},
        },
    }
    monkeypatch.setenv("RELEASE_MANIFEST", json.dumps(manifest))
    assert asyncio.run(inspect_database(url, require_head=True, drift=True))["head"]
    assert asyncio.run(record_release(url)) is True
    assert asyncio.run(record_release(url)) is False

    async def rows():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                return (
                    (
                        await connection.execute(
                            select(DeploymentAudit.__table__).order_by(
                                DeploymentAudit.id
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
        finally:
            await engine.dispose()

    records = asyncio.run(rows())
    assert len(records) == 2
    assert records[0]["kind"] == "schema_initialized"
    assert records[1]["id"] == 2
    assert records[1]["release_sha"] == manifest["sha"]
    assert records[1]["workflow_run_id"] == manifest["workflow_run_id"]
