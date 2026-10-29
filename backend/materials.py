"""Материалы проекта: загруженные пользователем файлы, которые Claude читает из materials/."""

from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path, PurePath

from sqlalchemy import select

from backend.db import Database
from backend.models import Project, ProjectFile


MATERIALS_DIR = "materials"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PROJECT_BYTES = 20 * 1024 * 1024
MAX_PROJECT_FILES = 20
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".md",
    ".png", ".jpg", ".jpeg", ".csv", ".xlsx",
}
_UNSAFE = re.compile(r"[^\w.\- ()]+", re.UNICODE)


class MaterialError(ValueError):
    pass


def safe_file_name(raw: str | None) -> str:
    """Только базовое имя без каталогов и служебных символов; расширение из списка."""
    name = unicodedata.normalize("NFC", PurePath((raw or "").replace("\\", "/")).name)
    name = _UNSAFE.sub("_", name).strip(" .")[:150]
    suffix = Path(name).suffix.lower()
    if not name or name.startswith(".") or suffix not in ALLOWED_EXTENSIONS:
        raise MaterialError("Этот тип файла не поддерживается")
    return name


async def project_materials(database: Database, conversation_id: str) -> dict[str, bytes] | None:
    """None — диалог не принадлежит проекту, его каталог materials/ не трогаем."""
    async with database.session() as session:
        project_id = await session.scalar(
            select(Project.id).where(Project.conversation_id == conversation_id)
        )
        if project_id is None:
            return None
        rows = await session.execute(
            select(ProjectFile.name, ProjectFile.content).where(ProjectFile.project_id == project_id)
        )
        return {name: content for name, content in rows}


def write_workspace_materials(workspace: Path, files: dict[str, bytes]) -> None:
    """В режиме cli каталог пересобирается целиком: удалённые файлы исчезают и у Claude."""
    target = workspace / MATERIALS_DIR
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    if not files:
        return
    target.mkdir()
    for name, data in files.items():
        (target / name).write_bytes(data)
