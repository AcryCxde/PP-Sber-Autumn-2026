"""Состояние восстановления `state.json`: всё, что runner должен пережить после рестарта."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, final

from ctrunner.fsio import write_json_atomic
from ctrunner.protocol import JsonValue, MessageCmd

STATE_VERSION: Final = 1
# Идентификатор подставляется в glob транскрипта: метасимволов в нём быть не может.
SESSION_ID_RE: Final = re.compile(r"[0-9A-Za-z_-]{1,64}")


class StateCorrupt(Exception):
    """`state.json` нельзя разобрать: решение о ходе без него было бы догадкой."""


def is_session_id(value: str) -> bool:
    return SESSION_ID_RE.fullmatch(value) is not None


@final
@dataclass(frozen=True, slots=True)
class OpenTurn:
    turn_id: str
    cmd: MessageCmd


@final
@dataclass(frozen=True, slots=True)
class PersistedState:
    session_id: str | None
    open_turn: OpenTurn | None
    last_sha: str | None
    autoresume_count: int  # подряд идущие автопродолжения открытого хода
    queue: tuple[MessageCmd, ...]

    def __post_init__(self) -> None:
        if self.session_id is not None and not is_session_id(self.session_id):
            raise ValueError("session_id has unexpected shape")
        if self.autoresume_count < 0:
            raise ValueError("autoresume_count must be non-negative")
        if self.open_turn is None and self.autoresume_count:
            raise ValueError("autoresume_count requires an open turn")


EMPTY: Final = PersistedState(None, None, None, 0, ())


def _cmd_json(cmd: MessageCmd) -> dict[str, JsonValue]:
    return {"id": cmd.id, "text": cmd.text}


def to_json(state: PersistedState) -> dict[str, JsonValue]:
    open_turn: JsonValue = (
        None
        if state.open_turn is None
        else {"turn_id": state.open_turn.turn_id, "cmd": _cmd_json(state.open_turn.cmd)}
    )
    return {
        "v": STATE_VERSION,
        "session_id": state.session_id,
        "open_turn": open_turn,
        "last_sha": state.last_sha,
        "autoresume_count": state.autoresume_count,
        "queue": [_cmd_json(c) for c in state.queue],
    }


def save_state(path: Path, state: PersistedState) -> None:
    write_json_atomic(path, to_json(state))


def load_state(path: Path) -> PersistedState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return EMPTY
    except (OSError, ValueError) as error:  # ValueError: и JSONDecodeError, и UnicodeDecodeError
        raise StateCorrupt(f"{path.name}: {error}") from error
    return parse_state(raw)


def parse_state(raw: object) -> PersistedState:
    """Граница: нетипизированный JSON → доменный тип. Любое несоответствие — `StateCorrupt`."""
    try:
        return _parse(raw)
    except ValueError as error:
        raise StateCorrupt(str(error)) from error


def _parse(raw: object) -> PersistedState:
    doc = _object(raw)
    if doc.get("v") != STATE_VERSION:
        raise ValueError(f"unsupported state version: {doc.get('v')!r}")
    open_raw = doc.get("open_turn")
    return PersistedState(
        session_id=_opt_str(doc, "session_id"),
        open_turn=None if open_raw is None else _open_turn(open_raw),
        last_sha=_opt_str(doc, "last_sha"),
        autoresume_count=_int(doc, "autoresume_count"),
        queue=tuple(_cmd(item) for item in _list(doc, "queue")),
    )


def _object(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("expected an object")
    return {str(k): v for k, v in raw.items()}


def _list(doc: dict[str, object], key: str) -> list[object]:
    value = doc.get(key)
    if not isinstance(value, list):
        raise ValueError(f"field {key!r} must be an array")
    return value


def _str(doc: dict[str, object], key: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"field {key!r} must be a non-empty string")
    return value


def _opt_str(doc: dict[str, object], key: str) -> str | None:
    return None if doc.get(key) is None else _str(doc, key)


def _int(doc: dict[str, object], key: str) -> int:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"field {key!r} must be an integer")
    return value


def _cmd(raw: object) -> MessageCmd:
    doc = _object(raw)
    return MessageCmd(_str(doc, "id"), _str(doc, "text"))


def _open_turn(raw: object) -> OpenTurn:
    doc = _object(raw)
    return OpenTurn(_str(doc, "turn_id"), _cmd(doc.get("cmd")))
