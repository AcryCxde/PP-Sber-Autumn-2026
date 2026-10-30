"""Однократный перенос истории из SQLite в пустую базу Postgres.

    python -m backend.transfer_sqlite /app/backend/data/history.db postgresql+asyncpg://...

Backend на время переноса должен быть остановлен. Исходный файл не меняется: миграции
до последней версии применяются к его временной копии. Целевая база доводится до той же
версии и должна быть пустой — повторный запуск после успешного переноса ничего не испортит.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Integer, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from backend import models  # noqa: F401 — регистрирует таблицы в metadata
from backend.db import Base, upgrade_database


BATCH_SIZE = 500


class TransferError(RuntimeError):
    pass


def _clean(value: Any, replaced: list[int]) -> Any:
    # Postgres не хранит NUL в тексте, SQLite хранит; символ заменяется на U+FFFD.
    if isinstance(value, str) and "\x00" in value:
        replaced[0] += 1
        return value.replace("\x00", "�")
    # SQLite отдаёт время без зоны, хотя записывалось оно в UTC.
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _row_counts(connection: AsyncConnection) -> dict[str, int]:
    return {
        table.name: await connection.scalar(select(func.count()).select_from(table))
        for table in Base.metadata.sorted_tables
    }


async def transfer(source_path: Path, target_url: str) -> dict[str, int]:
    if make_url(target_url).get_backend_name() != "postgresql":
        raise TransferError("Целевая база должна быть Postgres")
    if not source_path.is_file():
        raise TransferError(f"Нет файла {source_path}")

    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / "history.db"
        shutil.copy2(source_path, copy)
        source_url = f"sqlite+aiosqlite:///{copy.as_posix()}"
        await asyncio.to_thread(upgrade_database, source_url)
        await asyncio.to_thread(upgrade_database, target_url)

        source_engine = create_async_engine(source_url)
        target_engine = create_async_engine(target_url)
        try:
            async with target_engine.begin() as target:
                existing = {name: count for name, count in (await _row_counts(target)).items() if count}
                if existing:
                    raise TransferError(f"Целевая база не пуста: {existing}")

                replaced = [0]
                async with source_engine.connect() as source:
                    for table in Base.metadata.sorted_tables:
                        result = await source.stream(select(table))
                        async for rows in result.partitions(BATCH_SIZE):
                            batch = [
                                {key: _clean(value, replaced) for key, value in row._mapping.items()}
                                for row in rows
                            ]
                            await target.execute(table.insert(), batch)
                    expected = await _row_counts(source)

                # Явные id при вставке не двигают последовательности автоинкремента.
                for table in Base.metadata.sorted_tables:
                    for column in table.primary_key.columns:
                        if isinstance(column.type, Integer) and column.autoincrement is not False:
                            await target.execute(text(
                                f"SELECT setval(pg_get_serial_sequence('{table.name}', '{column.name}'), "
                                f"COALESCE((SELECT MAX({column.name}) FROM {table.name}), 0) + 1, false)"
                            ))

                actual = await _row_counts(target)
                if actual != expected:
                    raise TransferError(f"Число строк не совпало: {expected} → {actual}")
                if replaced[0]:
                    print(f"NUL-символы заменены в {replaced[0]} значениях", file=sys.stderr)
                return actual
        finally:
            await source_engine.dispose()
            await target_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="файл SQLite с историей")
    parser.add_argument("target", help="URL базы Postgres, postgresql+asyncpg://…")
    arguments = parser.parse_args()
    try:
        counts = asyncio.run(transfer(arguments.source, arguments.target))
    except TransferError as error:
        sys.exit(f"Перенос не выполнен: {error}")
    for name, count in counts.items():
        print(f"{name}: {count}")


if __name__ == "__main__":
    main()
