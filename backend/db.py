import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def ensure_sqlite_parent(url: str) -> None:
    parsed_url = make_url(url)
    if parsed_url.get_backend_name() != "sqlite":
        return

    database_path = parsed_url.database
    if not database_path or database_path == ":memory:":
        return

    Path(database_path).parent.mkdir(parents=True, exist_ok=True)


def upgrade_database(url: str) -> None:
    ensure_sqlite_parent(url)
    config = Config(str(Path(__file__).with_name("alembic.ini")))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")


class Database:
    def __init__(self, url: str):
        self.url = url

        engine_options = {}
        if url in {"sqlite+aiosqlite://", "sqlite+aiosqlite:///:memory:"}:
            engine_options["poolclass"] = StaticPool

        self.engine: AsyncEngine = create_async_engine(url, **engine_options)
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )

        if self.engine.url.get_backend_name() == "sqlite":
            self._enable_sqlite_foreign_keys()

    def _enable_sqlite_foreign_keys(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async def migrate(self) -> None:
        await asyncio.to_thread(upgrade_database, self.url)

    async def create_schema(self) -> None:
        ensure_sqlite_parent(self.url)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session
