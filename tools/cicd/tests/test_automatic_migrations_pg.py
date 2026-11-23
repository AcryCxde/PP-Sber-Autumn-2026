"""The candidate's unrestricted Alembic graph and ORM metadata must agree on PG16."""

import asyncio

from tools.cicd.tests.schema_pg_fixture import pg16  # noqa: F401


def test_candidate_head_matches_models_on_postgres(pg16):
    from backend.migrate import inspect_database

    pg16.migrate()
    url = f"postgresql+asyncpg://migrator@127.0.0.1:{pg16.port}/probe_test"
    result = asyncio.run(inspect_database(url, require_head=True, drift=True))
    assert result["head"] == result["current"]
