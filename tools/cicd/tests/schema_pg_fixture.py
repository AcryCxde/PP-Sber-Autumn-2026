"""Explicit opt-in, locally owned PG16 boundary; never uses host DB settings."""

import os
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

IMAGE = (
    "postgres@sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139"
)
ROOT = Path(__file__).resolve().parents[3]


def docker(*args: str, timeout: int = 30) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=timeout
    ).stdout.strip()


@dataclass(frozen=True)
class OwnedPG:
    container: str
    port: int

    async def connect(self, role: str = "postgres", database: str = "probe_test"):
        import asyncpg

        return await asyncpg.connect(
            host="127.0.0.1", port=self.port, user=role, database=database, timeout=5
        )

    def execute(self, sql: str, database: str = "probe_test") -> str:
        return docker(
            "exec",
            self.container,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            database,
            "-At",
            "-c",
            sql,
        )

    def migrate(self) -> None:
        from alembic import command
        from alembic.config import Config

        config = Config()
        config.set_main_option("script_location", str(ROOT / "backend/migrations"))
        config.attributes["database_url"] = (
            f"postgresql+asyncpg://migrator@127.0.0.1:{self.port}/probe_test"
        )
        command.upgrade(config, "head")


@pytest.fixture
def pg16() -> Iterator[OwnedPG]:
    if os.environ.get("SCHEMA_PG16_TESTS") != "1":
        pytest.skip("set SCHEMA_PG16_TESTS=1 for owned PostgreSQL 16.15 tests")
    container = None
    try:
        docker("image", "inspect", IMAGE)
        container = docker(
            "run",
            "--rm",
            "--detach",
            "--pull=never",
            "--name",
            "schema-probe-" + uuid.uuid4().hex,
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        )
        # Bounded TCP readiness avoids the entrypoint's temporary Unix server.
        docker(
            "exec",
            container,
            "sh",
            "-c",
            "i=0; until pg_isready -h 127.0.0.1 -U postgres -t 1 >/dev/null; do "
            "i=$((i+1)); test $i -lt 40 || exit 1; sleep 0.2; done",
            timeout=15,
        )
        port = int(docker("port", container, "5432/tcp").rsplit(":", 1)[1])
        pg = OwnedPG(container, port)
        assert pg.execute("SHOW server_version_num", "postgres") == "160015"
        pg.execute(
            "CREATE ROLE migrator LOGIN; CREATE ROLE probe LOGIN NOINHERIT;", "postgres"
        )
        pg.execute("CREATE DATABASE probe_test OWNER migrator", "postgres")
        pg.execute(
            "REVOKE CREATE, TEMP ON DATABASE probe_test FROM PUBLIC; "
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC; "
            "GRANT CREATE ON SCHEMA public TO migrator"
        )
        yield pg
    finally:
        if container is not None:
            docker("stop", "--time", "2", container, timeout=15)
