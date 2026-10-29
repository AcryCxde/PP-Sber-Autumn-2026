"""Материалы диалога: загруженные пользователем файлы, которые Claude читает из materials/.

У проекта это файлы проекта, у свободного диалога — приложенные к нему документы.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath

from sqlalchemy import select

from backend.db import Database
from backend.models import ConversationFile, Project, ProjectFile


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
    def __init__(self, message: str, status_code: int = 415) -> None:
        super().__init__(message)
        self.status_code = status_code


def safe_file_name(raw: str | None) -> str:
    """Только базовое имя без каталогов и служебных символов; расширение из списка."""
    name = unicodedata.normalize("NFC", PurePath((raw or "").replace("\\", "/")).name)
    name = _UNSAFE.sub("_", name).strip(" .")[:150]
    suffix = Path(name).suffix.lower()
    if not name or name.startswith(".") or suffix not in ALLOWED_EXTENSIONS:
        raise MaterialError("Этот тип файла не поддерживается")
    return name


def check_upload(raw_name: str | None, data: bytes, existing: Iterable[tuple[str, int]]) -> str:
    """Имя нового файла, если он проходит лимиты. existing — (имя, размер) уже загруженных.

    data читают на байт больше MAX_FILE_BYTES, чтобы не держать в памяти слишком большой файл.
    Файл с тем же именем заменяет прежний, поэтому его размер в сумму не входит.
    """
    name = safe_file_name(raw_name)
    if len(data) > MAX_FILE_BYTES:
        raise MaterialError("Файл больше 5 МБ", 413)
    if not data:
        raise MaterialError("Пустой файл", 422)
    others = [size for other, size in existing if other != name]
    if len(others) >= MAX_PROJECT_FILES:
        raise MaterialError("Уже загружено 20 файлов", 413)
    if sum(others) + len(data) > MAX_PROJECT_BYTES:
        raise MaterialError("Материалы больше 20 МБ", 413)
    return name


@dataclass(frozen=True)
class Materials:
    files: dict[str, bytes]
    # Свободный диалог: в отличие от промптов этапов проекта, запрос пользователя
    # ничего не говорит о materials/, и о файлах агенту сообщает materials_note.
    standalone: bool


async def conversation_materials(database: Database, conversation_id: str) -> Materials | None:
    """None — трогать materials/ не нужно: у свободного диалога нет приложенных файлов,
    а каталог с таким именем мог создать сам агент."""
    async with database.session() as session:
        project_id = await session.scalar(
            select(Project.id).where(Project.conversation_id == conversation_id)
        )
        if project_id is not None:
            rows = await session.execute(
                select(ProjectFile.name, ProjectFile.content)
                .where(ProjectFile.project_id == project_id)
            )
            return Materials({name: content for name, content in rows}, standalone=False)
        rows = await session.execute(
            select(ConversationFile.name, ConversationFile.content)
            .where(ConversationFile.conversation_id == conversation_id)
        )
        files = {name: content for name, content in rows}
        return Materials(files, standalone=True) if files else None


def materials_note(materials: Materials | None) -> str:
    """Приписка к запросу свободного диалога; в сохранённое сообщение пользователя не входит."""
    if materials is None or not materials.standalone or not materials.files:
        return ""
    names = ", ".join(sorted(materials.files))
    return (
        f"\n\n(Пользователь приложил файлы, они лежат в папке {MATERIALS_DIR}/ "
        f"рабочей директории: {names}. Итоговый документ сохрани текстовым файлом "
        f"(например, .md) в рабочей директории — пользователь скачает его из интерфейса.)"
    )


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
