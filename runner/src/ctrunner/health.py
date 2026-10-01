"""Файл живости для HEALTHCHECK: `docker ps` видит зависание и без гейтвея."""

import time
from pathlib import Path

from ctrunner.fsio import write_json_atomic
from ctrunner.liveness import Health, Phase


def write_health(path: Path, phase: Phase, health: Health, last_progress_at: float) -> None:
    write_json_atomic(
        path,
        {
            "phase": phase.value,
            "health": health.value,
            "last_progress_at": last_progress_at,
            "written_at": time.time(),
        },
    )
