"""Клиент gateway: runner сам подключается по WebSocket (`docs/2.4/protocol.md`).

Исходящий поток — хвост журнала `events.jsonl`, переведённый адаптером v0; входящий — команды,
которые проверяются и кладутся в файловый inbox. Так `Session` не знает о сети, а команда и событие
переживают обрыв и рестарт по тем же правилам, что и без gateway.
"""

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Final, assert_never, final

from websockets.asyncio.client import ClientConnection, connect

from ctrunner.diag import diag
from ctrunner.harness_diagnostic import SCHEMA, DiagnosticFeed, DiagnosticReceipts
from ctrunner.inbox import has_command, put
from ctrunner.protocol import (
    Command,
    ForkAnswerCmd,
    JsonValue,
    MessageCmd,
    ProtocolError,
    StopCmd,
    parse_command,
)
from ctrunner.v0 import Draft, Translator, parse_row

WIRE_VERSION: Final = 1
STABLE_CONNECTION_S: Final = 30.0


class DiagnosticAcks(dict[str, str]):
    """Connection-local ACK mailbox limited to actually outstanding private frames."""

    def __init__(self) -> None:
        super().__init__()
        self.awaiting: set[str] = set()


async def _record_diagnostic_acks(
    accepted: DiagnosticAcks,
    receipts: DiagnosticReceipts,
    pending: dict[str, dict[str, JsonValue]],
    attempts: dict[str, tuple[int, float]],
    leases: dict[str, float],
) -> None:
    for key in tuple(pending):
        status = accepted.pop(key, None)
        if status is not None:
            accepted.awaiting.discard(key)
            await asyncio.to_thread(receipts.record, key, status)
            pending.pop(key)
            attempts.pop(key, None)
            leases.pop(key, None)


def _rotate_diagnostics(
    accepted: DiagnosticAcks,
    pending: dict[str, dict[str, JsonValue]],
    attempts: dict[str, tuple[int, float]],
    leases: dict[str, float],
    now: float,
) -> bool:
    expired = False
    for key in tuple(pending):
        if now >= leases[key]:
            expired = True
            # No receipt is written: a later journal pass retries this ID.
            pending.pop(key)
            attempts.pop(key, None)
            leases.pop(key)
            accepted.awaiting.discard(key)
            accepted.pop(key, None)
    return expired


FRAME_ENVELOPE: Final = ("v", "type")


def hello_message(
    *, project_id: str, runner_version: str, session_id: str | None, last_seq: int
) -> dict[str, JsonValue]:
    return {
        "v": WIRE_VERSION, "type": "hello", "project_id": project_id,
        "runner_version": runner_version, "session_id": session_id, "last_seq": last_seq,
    }  # fmt: skip


def parse_welcome(raw: object) -> int:
    if not isinstance(raw, dict) or raw.get("type") != "welcome":
        raise ProtocolError("expected welcome")
    acked = raw.get("acked_seq")
    if isinstance(acked, bool) or not isinstance(acked, int) or acked < 0:
        raise ProtocolError("welcome.acked_seq must be a non-negative integer")
    return acked


def event_message(journal_seq: int, draft: Draft) -> dict[str, JsonValue]:
    """Вложенный `event`: `type` конверта и `type` события v0 иначе совпали бы по имени."""
    body: dict[str, JsonValue] = {
        "event_id": draft.event_id,
        "project_id": draft.project_id,
        "run_id": draft.run_id,
        "occurred_at": draft.occurred_at.isoformat().replace("+00:00", "Z"),
        "type": draft.type.value,
        "payload": draft.payload,
    }
    return {"v": WIRE_VERSION, "type": "event", "journal_seq": journal_seq, "event": body}


def heartbeat_message(health: Mapping[str, object]) -> dict[str, JsonValue] | None:
    phase, state = health.get("phase"), health.get("health")
    progress = health.get("last_progress_at")
    if not isinstance(phase, str) or not isinstance(state, str):
        return None
    if isinstance(progress, bool) or not isinstance(progress, int | float):
        return None
    return {"v": WIRE_VERSION, "type": "heartbeat", "phase": phase, "health": state,
            "last_progress_at": float(progress), "turn_id": None}  # fmt: skip


def command_ack(
    command_id: str | None, status: str, reason: str | None = None
) -> dict[str, JsonValue]:
    ack: dict[str, JsonValue] = {"v": WIRE_VERSION, "type": "command_ack", "id": command_id,
                                 "status": status}  # fmt: skip
    if reason is not None:
        ack["reason"] = reason
    return ack


@final
class JournalFeed:
    """Хвост журнала → драфты v0. `Translator` — свёртка с первой строки, поэтому читаем с начала,
    а наружу отдаём строки с `seq >= after_seq`: одна строка может дать несколько событий, и обрыв
    посреди неё оставляет `acked_seq` равным её номеру. Повтор безопасен — gateway дедуплицирует
    по `event_id`."""

    def __init__(self, path: Path, translator: Translator, after_seq: int) -> None:
        self._path = path
        self._translator = translator
        self._after = after_seq
        self._offset = 0

    def poll(self) -> list[tuple[int, tuple[Draft, ...]]]:
        try:
            with self._path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except FileNotFoundError:
            return []
        whole = chunk[: chunk.rfind(b"\n") + 1]  # недописанная строка ждёт следующего опроса
        out: list[tuple[int, tuple[Draft, ...]]] = []
        for line in whole.split(b"\n")[:-1]:
            self._offset += len(line) + 1
            if not line.strip():
                continue
            try:
                event = parse_row(json.loads(line))
                drafts = self._translator.feed(event)
            except ValueError as error:  # ProtocolError — его подкласс; ядовитая строка не должна
                diag("journal_row_skipped", error=str(error))  # останавливать поток навсегда
                continue
            if event.seq >= self._after and drafts:
                out.append((event.seq, drafts))
        return out


def command_to_raw(cmd: Command) -> dict[str, JsonValue]:
    """Проверенная команда → файл inbox: лишние поля конверта и мусор отбрасываются."""
    match cmd:
        case MessageCmd():
            raw: dict[str, JsonValue] = {"id": cmd.id, "kind": "message", "text": cmd.text}
            if cmd.roles:
                raw["roles"] = list(cmd.roles)
            if cmd.stage is not None:
                raw["stage"] = cmd.stage
            if cmd.input_files:
                raw["input_files"] = list(cmd.input_files)
            return raw
        case ForkAnswerCmd():
            return {"id": cmd.id, "kind": "fork_answer", "fork_id": cmd.fork_id,
                    "answers": dict(cmd.answers)}  # fmt: skip
        case StopCmd():
            return {"id": cmd.id, "kind": "stop"}
        case _:
            assert_never(cmd)


def accept_command(
    frame: Mapping[str, object],
    *,
    inbox_dir: Path,
    fork_questions: Callable[[str], tuple[str, ...] | None],
) -> dict[str, JsonValue]:
    """Команда gateway → `command_ack`. Принятая команда durable (файл в inbox) до ответа."""
    body = {k: v for k, v in frame.items() if k not in FRAME_ENVELOPE}
    raw_id = body.get("id")
    try:
        cmd = parse_command(body)
    except ProtocolError as error:
        return command_ack(raw_id if isinstance(raw_id, str) else None, "rejected", str(error))
    if has_command(inbox_dir, cmd.id):  # повтор после потерянного ack: тот же id, тот же исход
        return command_ack(cmd.id, "accepted")
    match cmd:
        case ForkAnswerCmd():
            questions = fork_questions(cmd.fork_id)
            if questions is None:
                return command_ack(cmd.id, "rejected", "unknown_fork")
            if set(cmd.answers) != set(questions):
                return command_ack(cmd.id, "rejected", "answers_mismatch")
        case MessageCmd() | StopCmd():
            pass
        case _:
            assert_never(cmd)
    put(inbox_dir, command_to_raw(cmd))
    return command_ack(cmd.id, "accepted")


@final
class GatewayClient:
    """Соединение с gateway: `hello` → `welcome` → события, heartbeat и команды до обрыва.

    Живёт отдельной задачей и не роняет Session: любой сбой сети — реконнект с задержкой.
    Токен в журнал и diag не попадает (в diag только тип ошибки).
    """

    def __init__(  # noqa: PLR0913 — зависимости передаются явно
        self,
        *,
        url: str,
        token: str,
        project_id: str,
        runner_version: str,
        session_id: str | None,
        journal: Path,
        health_file: Path,
        inbox_dir: Path,
        project_root: str,
        last_seq: Callable[[], int],
        fork_questions: Callable[[str], tuple[str, ...] | None],
        poll_s: float = 0.5,
        heartbeat_s: float = 10.0,
        backoff: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0),
    ) -> None:
        self._url = url
        self._token = token
        self._project_id = project_id
        self._runner_version = runner_version
        self._session_id = session_id
        self._journal = journal
        self._health_file = health_file
        self._inbox_dir = inbox_dir
        self._project_root = project_root
        self._last_seq = last_seq
        self._fork_questions = fork_questions
        self._poll_s = poll_s
        self._heartbeat_s = heartbeat_s
        self._backoff = backoff
        self._failures = 0
        self._draining = asyncio.Event()
        self._drain_deadline: float | None = None
        self._public_flushed = asyncio.Event()

    def drain(self) -> None:
        """Выход runner: дослать хвост журнала и закончить `run` без реконнекта."""
        if self._drain_deadline is None:
            # Reserve time for socket teardown within RunningGateway.stop's five seconds.
            self._drain_deadline = time.monotonic() + 4.0
        self._draining.set()

    async def run(self) -> None:
        while not self._draining.is_set():
            started = time.monotonic()
            try:
                await self._connection()
            except Exception as error:  # граница задачи: любой сбой не должен гасить поток навсегда
                diag("gateway_disconnected", error=type(error).__name__)
            if time.monotonic() - started >= STABLE_CONNECTION_S:
                self._failures = 0  # живое соединение; мгновенно закрывающий gateway не сбрасывает
            if self._draining.is_set():
                return
            delay = self._backoff[min(self._failures, len(self._backoff) - 1)]
            self._failures += 1
            with suppress(TimeoutError):
                await asyncio.wait_for(self._draining.wait(), delay)

    async def _connection(self) -> None:
        headers = {"Authorization": f"Bearer {self._token}"}
        async with connect(self._url, additional_headers=headers, open_timeout=10) as ws:
            hello = hello_message(
                project_id=self._project_id, runner_version=self._runner_version,
                session_id=self._session_id, last_seq=self._last_seq(),
            )  # fmt: skip
            hello["capabilities"] = {"harness_diagnostic_rejection_v1": True}
            await ws.send(json.dumps(hello, ensure_ascii=False))
            async with asyncio.timeout(10):
                welcome = json.loads(await ws.recv())
                acked = parse_welcome(welcome)
            diag("gateway_connected", acked_seq=acked)
            translator = Translator(self._project_id, self._project_root)
            feed = JournalFeed(self._journal, translator, acked)
            capabilities = welcome.get("capabilities")
            supports_rejection = (isinstance(capabilities, dict)
                                  and capabilities.get("harness_diagnostic_rejection_v1") is True)
            accepted_diagnostics = DiagnosticAcks()
            self._public_flushed.clear()
            public_task = asyncio.create_task(self._send_events(ws, feed))
            diagnostic_task: asyncio.Task[None] | None = None
            drain_task = asyncio.create_task(self._draining.wait())
            tasks = [
                public_task,
                drain_task,
                asyncio.create_task(self._send_heartbeats(ws)),
                asyncio.create_task(self._receive(ws, accepted_diagnostics,
                                                   supports_rejection=supports_rejection)),
            ]
            capabilities = welcome.get("capabilities")
            if isinstance(capabilities, dict) and capabilities.get("harness_diagnostic_v1") is True:
                diagnostic_task = asyncio.create_task(
                    self._send_diagnostics(ws, accepted_diagnostics)
                )
                tasks.append(diagnostic_task)
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if self._draining.is_set() and self._drain_deadline is not None:
                    flush = [public_task]
                    if diagnostic_task is not None:
                        flush.append(diagnostic_task)
                    remaining = max(0.0, self._drain_deadline - time.monotonic())
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            asyncio.gather(*(asyncio.shield(task) for task in flush)), remaining
                        )
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for task in done:
                task.result()  # обрыв соединения поднимается как исключение и ведёт к реконнекту

    async def _send_events(self, ws: ClientConnection, feed: JournalFeed) -> None:
        while True:
            last = self._draining.is_set()  # читаем до опроса: хвост, записанный до drain, уйдёт
            for seq, drafts in await asyncio.to_thread(feed.poll):
                for draft in drafts:
                    await ws.send(json.dumps(event_message(seq, draft), ensure_ascii=False))
            if last:
                self._public_flushed.set()
                return
            with suppress(TimeoutError):
                await asyncio.wait_for(self._draining.wait(), self._poll_s)

    async def _send_diagnostics(self, ws: ClientConnection, accepted: DiagnosticAcks) -> None:
        try:
            receipts = await asyncio.to_thread(DiagnosticReceipts, self._journal)
            await self._diagnostic_loop(ws, accepted, receipts)
        except (OSError, sqlite3.Error) as error:
            # Optional private storage failure must not disconnect legacy delivery.
            diag("gateway_diagnostics_unavailable", error=type(error).__name__)
            await self._public_flushed.wait()

    async def _diagnostic_loop(
        self, ws: ClientConnection, accepted: DiagnosticAcks, receipts: DiagnosticReceipts
    ) -> None:
        feed = DiagnosticFeed(self._journal, self._project_id, self._project_root)
        pending: dict[str, dict[str, JsonValue]] = {}
        attempts: dict[str, tuple[int, float]] = {}
        leases: dict[str, float] = {}
        # Bound one hot-window residency, not a diagnostic's lifetime. The retained
        # journal is revisited after EOF, so old peers cannot permanently block its tail.
        lease_s = min(30.0, max(1.0, self._poll_s * 4))
        drain_started = False
        revisit = False
        while True:
            draining = self._draining.is_set()
            if draining and not drain_started:
                attempts.clear()  # Shutdown must not inherit the normal thirty-second backoff.
                drain_started = True
            if (draining and self._drain_deadline is not None
                    and time.monotonic() >= self._drain_deadline):
                return
            await _record_diagnostic_acks(accepted, receipts, pending, attempts, leases)
            now = time.monotonic()
            if not draining:
                revisit = _rotate_diagnostics(accepted, pending, attempts, leases, now) or revisit
                if revisit and not pending and await asyncio.to_thread(feed.caught_up):
                    feed.rewind()
                    revisit = False
            for frame in await asyncio.to_thread(feed.poll, 256 - len(pending)):
                key = str(frame["diagnostic_id"])
                if not await asyncio.to_thread(receipts.contains, key):
                    pending[key] = frame
                    leases[key] = now + lease_s
            if (draining and self._public_flushed.is_set() and not pending
                    and await asyncio.to_thread(feed.caught_up)):
                return
            now = time.monotonic()
            for key, frame in pending.items():
                count, due = attempts.get(key, (0, 0.0))
                if now >= due:
                    accepted.awaiting.add(key)
                    await ws.send(json.dumps(frame, ensure_ascii=False))
                    delay = min(30.0, self._poll_s * 2 ** min(count, 10))
                    attempts[key] = (count + 1, now + (min(delay, 0.05) if draining else delay))
            if draining:
                await asyncio.sleep(min(self._poll_s, 0.05))
            else:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._draining.wait(), self._poll_s)

    async def _send_heartbeats(self, ws: ClientConnection) -> None:
        while True:
            beat = await asyncio.to_thread(self._read_heartbeat)
            if beat is not None:
                await ws.send(json.dumps(beat))
            await asyncio.sleep(self._heartbeat_s)

    def _read_heartbeat(self) -> dict[str, JsonValue] | None:
        try:
            return heartbeat_message(json.loads(self._health_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None  # файла ещё нет или он пишется: следующий heartbeat через период

    async def _receive(self, ws: ClientConnection, accepted_diagnostics: DiagnosticAcks,
                       *, supports_rejection: bool = False) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except ValueError:
                diag("gateway_bad_frame")
                continue
            if not isinstance(frame, dict):
                continue
            if (frame.get("type") == "harness.diagnostic_ack" and frame.get("v") == 1
                    and frame.get("schema") == SCHEMA
                    and (frame.get("status") == "accepted" or
                         (supports_rejection and frame.get("status") == "rejected"))
                    and isinstance(frame.get("diagnostic_id"), str)):
                if frame["diagnostic_id"] in accepted_diagnostics.awaiting:
                    accepted_diagnostics[frame["diagnostic_id"]] = frame["status"]
                continue
            if frame.get("type") != "command":
                continue
            # Маленький файл в inbox: вызов короткий, а проверка реестра развилок выполняется
            # в потоке цикла событий, где живёт Session.
            ack = accept_command(
                frame, inbox_dir=self._inbox_dir, fork_questions=self._fork_questions
            )
            await ws.send(json.dumps(ack, ensure_ascii=False))
