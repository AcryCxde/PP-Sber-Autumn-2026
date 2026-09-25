"""Оболочка вокруг долгоживущего клиента SDK: читатель сообщений, команды, heartbeat,
сторож PreToolUse и развилки AskUserQuestion. Решения принимает чистый `turn`.

Всё выполняется в одном цикле событий; состояние меняется без `await` между чтением и записью,
поэтому блокировки не нужны.
"""

import asyncio
import errno
import os
import sys
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Callable
from enum import IntEnum
from pathlib import Path
from typing import Any, Protocol, assert_never, final

from claude_agent_sdk import (
    HookContext,
    HookInput,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)
from claude_agent_sdk.types import SyncHookJSONOutput

from ctrunner.diag import diag
from ctrunner.eventlog import Event
from ctrunner.guard import Allow, Deny, Policy, Verdict, check
from ctrunner.health import write_health
from ctrunner.inbox import Inbox
from ctrunner.liveness import Health, Phase, assess
from ctrunner.protocol import EventKind, ForkAnswerCmd, JsonValue, MessageCmd, StopCmd
from ctrunner.redact import Redactor
from ctrunner.sdkevents import normalize, to_json
from ctrunner.turn import (
    Effect,
    Idle,
    SendPrompt,
    TurnDone,
    TurnFailedFx,
    TurnState,
    Working,
    next_queued,
    on_command,
    on_fork_closed,
    on_fork_open,
    on_message,
)


class EventSink(Protocol):
    def append(self, kind: EventKind, turn_id: str | None, payload: JsonValue) -> Event: ...


class SdkClient(Protocol):
    async def query(self, prompt: str) -> None: ...

    def receive_messages(self) -> AsyncIterator[Message]: ...


class Exit(IntEnum):
    STOPPED = 0
    CRASHED = 1  # Docker перезапустит контейнер


def _realpath(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _new_id() -> str:
    return uuid.uuid4().hex


@final
class Session:
    def __init__(  # noqa: PLR0913 — зависимости оболочки передаются явно
        self,
        *,
        log: EventSink,
        redactor: Redactor,
        inbox: Inbox,
        policy: Policy,
        health_file: Path,
        stall_after_s: float,
        poll_s: float = 1.0,
        heartbeat_s: float = 10.0,
        resolve: Callable[[Path], Path] = _realpath,
    ) -> None:
        self._log = log
        self._redactor = redactor
        self._inbox = inbox
        self._policy = policy
        self._health_file = health_file
        self._stall_after_s = stall_after_s
        self._poll_s = poll_s
        self._heartbeat_s = heartbeat_s
        self._resolve = resolve
        self._state: TurnState = Idle(queue=())
        self._forks: dict[str, asyncio.Future[dict[str, str]]] = {}
        self._progress_mono = time.monotonic()
        self._progress_wall = time.time()
        self._stop = asyncio.Event()
        self._fatal: BaseException | None = None
        self._fatal_set = asyncio.Event()

    # --- жизненный цикл -------------------------------------------------------------------

    async def run(self, client: SdkClient) -> Exit:
        tasks = [
            asyncio.create_task(self._read(client)),
            asyncio.create_task(self._commands(client)),
            asyncio.create_task(self._heartbeat()),
            asyncio.create_task(self._wait_stop()),
            asyncio.create_task(self._wait_fatal()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for fork in self._forks.values():
                fork.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        errors = [e for t in done if (e := t.exception()) is not None]
        if errors:
            return self.crash(errors[0])
        if any(t.result() is Exit.CRASHED for t in done):
            return self.crash(None)
        self._write_health()
        return Exit.STOPPED

    def request_stop(self) -> None:
        """SIGTERM от `docker stop`."""
        self._stop.set()

    def crash(self, error: BaseException | None) -> Exit:
        """Процессная граница: зафиксировать отказ (best-effort) и выйти с ненулевым кодом."""
        reason = "sdk_crashed"
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            reason = "disk_full"
        detail = "".join(traceback.format_exception(error)) if error is not None else None
        diag("runner_crashed", reason=reason, traceback=self._redactor.apply(detail))
        try:
            write_health(self._health_file, Phase.FAILED, Health.CRASHED, self._progress_wall)
        except OSError as health_error:
            diag("health_write_failed", error=health_error.strerror)
        try:
            self._log.append(EventKind.TURN_FAILED, self._turn_id(), {"reason": reason})
        except OSError as log_error:
            diag("event_write_failed", kind=EventKind.TURN_FAILED.value, error=log_error.strerror)
        return Exit.CRASHED

    def sdk_stderr(self, line: str) -> None:
        print(self._redactor.text(line), file=sys.stderr)

    # --- задачи ---------------------------------------------------------------------------

    async def _read(self, client: SdkClient) -> Exit:
        async for msg in client.receive_messages():
            self._touch()
            payload = normalize(msg)
            if payload is not None:
                self._log.append(EventKind.SDK, self._turn_id(), payload)
            self._state, effects = on_message(self._state, msg)
            await self._apply(client, effects)
        return Exit.CRASHED  # поток SDK не заканчивается, пока жив процесс CLI

    async def _commands(self, client: SdkClient) -> Exit:
        while True:
            for cmd in await asyncio.to_thread(self._inbox.take):
                match cmd:
                    case MessageCmd():
                        self._state, effects = on_command(self._state, cmd, turn_id=_new_id())
                        await self._apply(client, effects)
                    case ForkAnswerCmd():
                        self._answer(cmd)
                    case StopCmd():
                        return Exit.STOPPED
                    case _:
                        assert_never(cmd)
            await asyncio.sleep(self._poll_s)

    async def _heartbeat(self) -> Exit:
        while True:
            self._write_health()
            await asyncio.sleep(self._heartbeat_s)

    async def _wait_stop(self) -> Exit:
        await self._stop.wait()
        return Exit.STOPPED

    async def _wait_fatal(self) -> Exit:
        await self._fatal_set.wait()
        if self._fatal is not None:
            raise self._fatal
        return Exit.CRASHED

    # --- эффекты --------------------------------------------------------------------------

    async def _apply(self, client: SdkClient, effects: tuple[Effect, ...]) -> None:
        for effect in effects:
            match effect:
                case SendPrompt(turn_id=turn_id, text=text):
                    self._touch()
                    self._log.append(EventKind.TURN_STARTED, turn_id, {"prompt": text})
                    await client.query(text)
                case TurnDone(turn_id=turn_id):
                    self._log.append(EventKind.TURN_COMPLETED, turn_id, {})
                    await self._start_queued(client)
                case TurnFailedFx(turn_id=turn_id, reason=reason):
                    self._log.append(EventKind.TURN_FAILED, turn_id, {"reason": reason})
                    await self._start_queued(client)
                case _:
                    assert_never(effect)

    async def _start_queued(self, client: SdkClient) -> None:
        if isinstance(self._state, Idle):
            self._state, effects = next_queued(self._state, turn_id=_new_id())
            await self._apply(client, effects)

    def _answer(self, cmd: ForkAnswerCmd) -> None:
        fork = self._forks.get(cmd.fork_id)
        if fork is None or fork.done():
            diag("fork_answer_dropped", fork_id=cmd.fork_id, command_id=cmd.id)
            return
        fork.set_result(cmd.answers)

    # --- обратные вызовы SDK --------------------------------------------------------------

    async def pre_tool_use(
        self, hook_input: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        self._touch()
        if hook_input["hook_event_name"] != "PreToolUse":
            return {}
        verdict = self._verdict(
            hook_input["tool_name"], hook_input["tool_input"], hook_input["cwd"]
        )
        match verdict:
            case Allow():
                return {}
            case Deny(path=path, reason=reason):
                payload: dict[str, JsonValue] = {
                    "tool": hook_input["tool_name"],
                    "path": path,
                    "agent_id": hook_input.get("agent_id"),
                }
                self._record(EventKind.ACCESS_DENIED, payload)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            case _:
                assert_never(verdict)

    def _verdict(self, tool: str, tool_input: dict[str, Any], cwd: str) -> Verdict:
        try:
            return check(tool, tool_input, Path(cwd), self._policy, self._resolve)
        except Exception as error:  # сбой сторожа не должен пропускать вызов
            return Deny("?", f"сторож не смог проверить вызов: {type(error).__name__}")

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        self._touch()
        if tool_name != "AskUserQuestion":
            return PermissionResultAllow()
        fork_id = _new_id()
        fork: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
        self._forks[fork_id] = fork
        self._fork_opened()
        try:
            questions = to_json(tool_input.get("questions", []))
            self._record(EventKind.FORK_QUESTION, {"fork_id": fork_id, "questions": questions})
            answers = await fork  # без таймаута: человек думает, это не зависание
        finally:
            del self._forks[fork_id]
            self._fork_closed()
        self._touch()
        answered: dict[str, JsonValue] = {"fork_id": fork_id, "answers": {**answers}}
        self._record(EventKind.FORK_ANSWERED, answered)
        return PermissionResultAllow(updated_input={**tool_input, "answers": answers})

    # --- вспомогательное ------------------------------------------------------------------

    def _record(self, kind: EventKind, payload: JsonValue) -> None:
        """Запись из обратного вызова SDK: ошибку диска нельзя вернуть в SDK, она роняет runner."""
        try:
            self._log.append(kind, self._turn_id(), payload)
        except OSError as error:
            if self._fatal is None:
                self._fatal = error
                self._fatal_set.set()

    def _fork_opened(self) -> None:
        if isinstance(self._state, Working):
            self._state = on_fork_open(self._state)

    def _fork_closed(self) -> None:
        # Развилка могла открыться вне хода (Idle) и тогда не учитывалась.
        if isinstance(self._state, Working) and self._state.forks > 0:
            self._state = on_fork_closed(self._state)

    def _turn_id(self) -> str | None:
        return self._state.turn_id if isinstance(self._state, Working) else None

    def _touch(self) -> None:
        self._progress_mono = time.monotonic()
        self._progress_wall = time.time()

    def _write_health(self) -> None:
        phase = self._state.phase
        health = assess(
            phase,
            last_progress_at=self._progress_mono,
            now=time.monotonic(),
            stall_after_s=self._stall_after_s,
        )
        write_health(self._health_file, phase, health, self._progress_wall)
