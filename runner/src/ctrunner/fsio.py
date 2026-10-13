"""Атомарная запись файлов: читатель видит либо старую версию, либо новую целиком."""

import json
import os
import tempfile
from pathlib import Path

from ctrunner.protocol import JsonValue


def write_json_atomic(path: Path, value: JsonValue) -> None:
    # Сериализация до создания tmp: ошибка типа не оставляет мусора рядом с файлом.
    data = json.dumps(value, ensure_ascii=False).encode()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
