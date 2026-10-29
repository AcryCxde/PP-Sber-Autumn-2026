"""База для тестов: SQLite-файл по умолчанию, отдельная база Postgres при TEST_POSTGRES_URL.

TEST_POSTGRES_URL указывает на сервер, где у пользователя есть право CREATE DATABASE, например
postgresql+asyncpg://test:test@127.0.0.1:55432/test. Каждый вызов создаёт пустую базу,
conftest удаляет их в конце сессии.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import make_url


POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
created_databases: list[str] = []


def _admin_dsn() -> str:
    url = make_url(POSTGRES_URL).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def _run_admin(statements: list[str]) -> None:
    """Выполняет DDL в своём потоке: тесты уже работают внутри event loop."""
    import asyncpg

    async def run() -> None:
        connection = await asyncpg.connect(_admin_dsn())
        try:
            for statement in statements:
                await connection.execute(statement)
        finally:
            await connection.close()

    errors: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(run())
        except BaseException as error:  # noqa: BLE001 — передаём в вызывающий поток
            errors.append(error)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


def database_url(sqlite_path: Path) -> str:
    if not POSTGRES_URL:
        return f"sqlite+aiosqlite:///{sqlite_path.as_posix()}"
    name = f"test_{uuid4().hex}"
    _run_admin([f'CREATE DATABASE "{name}"'])
    created_databases.append(name)
    return make_url(POSTGRES_URL).set(database=name).render_as_string(hide_password=False)


def drop_created_databases() -> None:
    if created_databases:
        _run_admin([f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)' for name in created_databases])
        created_databases.clear()
