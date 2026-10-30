"""Промпт хода и запись о начале хода из команды `message`. Без ввода-вывода."""

import re
from typing import Final

from ctrunner.protocol import JsonValue, MessageCmd

BLOCK_TITLE: Final = "Роли / Этап / Входные файлы:"
_LINE_BREAK_RE: Final = re.compile(r"\r\n|[\n\r\x0b\x0c\x85  ]")


def _one_line(value: str) -> str:
    """Роль и этап приходят от клиента: перевод строки не должен начинать новую строку промпта."""
    return _LINE_BREAK_RE.sub(r"\\n", value)


def task_prompt(cmd: MessageCmd) -> str:
    """Текст команды, а при заданных ролях, этапе или файлах — ещё и отдельный блок под ним."""
    lines: list[str] = []
    if cmd.roles:
        lines.append(f"Роли: {', '.join(_one_line(r) for r in cmd.roles)}")
    if cmd.stage is not None:
        lines.append(f"Этап: {_one_line(cmd.stage)}")
    if cmd.input_files:
        lines.append("Входные файлы (относительно /workspace/project):")
        lines.extend(f"- {_one_line(p)}" for p in cmd.input_files)
    if not lines:
        return cmd.text
    return "\n".join([cmd.text, "", BLOCK_TITLE, *lines])


def turn_started_payload(prompt: str, cmd: MessageCmd) -> dict[str, JsonValue]:
    """Поля входа пишутся в журнал только если заданы: прежние записи не меняются."""
    payload: dict[str, JsonValue] = {"prompt": prompt}
    if cmd.roles:
        payload["roles"] = list(cmd.roles)
    if cmd.stage is not None:
        payload["stage"] = cmd.stage
    if cmd.input_files:
        payload["input_files"] = list(cmd.input_files)
    return payload
