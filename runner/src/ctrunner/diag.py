"""Диагностика runner в stderr: стабильное имя события, переменные — отдельными полями."""

import json
import sys

from ctrunner.protocol import JsonValue


def diag(event: str, /, **fields: JsonValue) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), file=sys.stderr)
