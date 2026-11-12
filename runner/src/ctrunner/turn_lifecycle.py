"""Shared journal turn lifecycle; no roles, paths or SDK content retained."""

from dataclasses import dataclass
from enum import StrEnum

from ctrunner.eventlog import Event
from ctrunner.protocol import EventKind, ProtocolError


class FailReason(StrEnum):
    RESULT_ERROR = "result_error"
    SDK_CRASHED = "sdk_crashed"
    DISK_FULL = "disk_full"
    CHECKPOINT_FAILED = "checkpoint_failed"
    RESUME_LIMIT = "resume_limit"
    STATE_CORRUPT = "state_corrupt"


@dataclass(frozen=True, slots=True)
class LifecycleChange:
    started: str | None = None
    closed: tuple[str, ...] = ()
    interrupted: str | None = None
    reason: FailReason | None = None


class TurnLiveness:
    def __init__(self) -> None:
        # Insertion order preserves state_corrupt public event order. Closed IDs
        # are tombstones: a duplicate start must never reopen a terminal turn.
        self._turns: dict[str, bool] = {}

    def live(self, run: str | None) -> bool:
        return run is not None and self._turns.get(run) is True

    def feed(self, event: Event) -> LifecycleChange:
        run = event.turn_id
        if event.kind is EventKind.TURN_STARTED:
            if run is not None and run not in self._turns:
                self._turns[run] = True
                return LifecycleChange(started=run)
        elif event.kind is EventKind.TURN_COMPLETED:
            if run is not None and self.live(run):
                self._turns[run] = False
                return LifecycleChange(closed=(run,))
        elif event.kind is EventKind.TURN_FAILED:
            payload = event.payload
            if not isinstance(payload, dict):
                raise ProtocolError("payload must be an object")
            raw = payload.get("reason")
            try:
                if not isinstance(raw, str) or not raw:
                    raise ProtocolError("field 'reason' must be a non-empty string")
                reason = FailReason(raw)
            except ValueError as error:
                raise ProtocolError(f"unknown turn_failed reason: {raw!r}") from error
            if reason is FailReason.STATE_CORRUPT:
                closed = tuple(key for key, live in self._turns.items() if live)
                for key in closed:
                    self._turns[key] = False
                return LifecycleChange(closed=closed, reason=reason)
            if run is not None and self.live(run):
                if payload.get("fatal") is True or reason is FailReason.SDK_CRASHED:
                    return LifecycleChange(interrupted=run, reason=reason)
                self._turns[run] = False
                return LifecycleChange(closed=(run,), reason=reason)
        return LifecycleChange()
