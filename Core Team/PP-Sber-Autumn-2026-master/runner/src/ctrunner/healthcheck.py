"""HEALTHCHECK образа: код 0 только при свежем heartbeat с `health == ok`."""

import json
import sys
import time
from enum import StrEnum
from pathlib import Path
from typing import Final

from ctrunner.config import HEALTH_FILE
from ctrunner.liveness import Health

# Дольше этого без heartbeat гейтвей тоже считает сессию упавшей.
MAX_AGE_S: Final = 30.0


class Status(StrEnum):
    OK = "ok"
    STALLED = "stalled"
    CRASHED = "crashed"
    STALE = "stale"  # runner перестал писать heartbeat
    MISSING = "missing"
    INVALID = "invalid"


def status(path: Path, *, now: float) -> Status:
    parsed = _read(path)
    if isinstance(parsed, Status):
        return parsed
    health, written_at = parsed
    if now - written_at > MAX_AGE_S:
        return Status.STALE
    return Status(health.value)


def _read(path: Path) -> tuple[Health, float] | Status:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Status.MISSING
    except (OSError, ValueError):
        return Status.INVALID
    written_at = doc.get("written_at") if isinstance(doc, dict) else None
    if isinstance(written_at, bool) or not isinstance(written_at, int | float):
        return Status.INVALID
    try:
        return Health(doc.get("health")), float(written_at)
    except ValueError:
        return Status.INVALID


def verdict(path: Path, *, now: float) -> int:
    return 0 if status(path, now=now) is Status.OK else 1


def cli() -> None:
    current = status(HEALTH_FILE, now=time.time())
    print(current.value)  # попадает в `docker inspect` → State.Health.Log
    sys.exit(0 if current is Status.OK else 1)
