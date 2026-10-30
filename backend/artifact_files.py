"""Сохранение файла, созданного агентом: байты — в объектное хранилище, запись и превью — в базу."""

from __future__ import annotations

import logging
from typing import Any

from backend.models import Event
from backend.repository import append_artifact
from backend.storage import StorageError, store_file, text_preview


logger = logging.getLogger(__name__)


async def save_agent_file(app_state: Any, run_id: str, path: str, data: bytes) -> Event:
    settings = app_state.settings
    storage = getattr(app_state, "storage", None)
    stored = None
    if storage is not None and len(data) <= settings.artifact_max_storage_bytes:
        try:
            stored = await store_file(storage, run_id, path, data)
        except StorageError:
            # Хранилище недоступно: запуск не роняем, текстовый файл останется скачиваемым из базы.
            logger.exception("Object storage failed for %s of run %s", path, run_id)
    preview, too_large = text_preview(data, settings.artifact_max_download_bytes)
    async with app_state.database.session() as session:
        return await append_artifact(
            session,
            run_id,
            path=path,
            content=preview,
            size_bytes=len(data),
            # truncated: целиком файл скачать нельзя — ни объекта, ни полного текста в базе.
            truncated=stored is None and (too_large or preview is None),
            num_lines=preview.count("\n") + 1 if preview is not None else None,
            stored=stored,
        )
