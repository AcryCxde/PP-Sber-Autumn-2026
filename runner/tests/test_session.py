import asyncio
import json
import os
import threading
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    HookContext,
    Message,
    PermissionResultAllow,
    PreToolUseHookInput,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

from ctrunner.checkpoint import CheckpointError, CheckpointFailure
from ctrunner.eventlog import Event, EventLog, kinds_of_turn
from ctrunner.guard import Policy
from ctrunner.inbox import Inbox, put
from ctrunner.protocol import EventKind, JsonValue, MessageCmd
from ctrunner.recovery import Continue, Finalize, GiveUp, ResumeMode
from ctrunner.redact import Redactor
from ctrunner.session import Exit, Session
from ctrunner.state import EMPTY, OpenTurn, PersistedState, load_state, save_state

TOKEN = "sk-test-0123456789"
ETC_PASSWD = os.path.realpath("/etc/passwd")  # на macOS это /private/etc/passwd
SHA = "a" * 40


class FakeCommit:
    """Вместо git: записывает ходы, при `error` отказывает как диск."""

    def __init__(self) -> None:
        self.turns: list[str] = []
        self.error: CheckpointError | None = None

    def __call__(self, turn_id: str) -> str:
        if self.error is not None:
            raise self.error
        self.turns.append(turn_id)
        return SHA


def result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
    )


def assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


class FakeClient:
    """Отвечает на каждый query заранее заданным сценарием; `None` в очереди — SDK завершился."""

    def __init__(self, script: Sequence[Message]) -> None:
        self.script = script
        self.prompts: list[str] = []
        self.queue: asyncio.Queue[Message | None] = asyncio.Queue()

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)
        for m in self.script:
            self.queue.put_nowait(m)

    async def receive_messages(self) -> AsyncIterator[Message]:
        while (m := await self.queue.get()) is not None:
            yield m


class Observed:
    """Настоящий журнал плюс сигнал о каждом виде события — ожидание без опроса."""

    def __init__(self, log: EventLog) -> None:
        self.log = log
        self.seen: defaultdict[EventKind, asyncio.Event] = defaultdict(asyncio.Event)

    def append(self, kind: EventKind, turn_id: str | None, payload: JsonValue) -> Event:
        event = self.log.append(kind, turn_id, payload)
        self.seen[kind].set()
        return event


class Env:
    def __init__(
        self, root: Path, state: PersistedState = EMPTY, commit: Callable[[str], str] | None = None
    ) -> None:
        self.inbox_dir = root / "inbox"
        self.events = root / "events.jsonl"
        self.health = root / "health.json"
        self.project = root / "project"
        self.project.mkdir()
        redactor = Redactor.of([TOKEN])
        self.log = EventLog.open(self.events, redactor, time.time)
        self.state_file = root / "state.json"
        self.commit = FakeCommit()
        self.saved: asyncio.Queue[tuple[PersistedState, int]] = asyncio.Queue()
        self.kinds_at_save: list[tuple[bool, list[str]]] = []  # (ход закрыт?, журнал на тот момент)
        self.observed = Observed(self.log)
        self.session = Session(
            log=self.observed,
            redactor=redactor,
            inbox=Inbox(self.inbox_dir),
            policy=Policy(roots=(self.project,), bash_system=()),
            health_file=self.health,
            stall_after_s=720.0,
            poll_s=0.01,
            heartbeat_s=0.01,
            state=state,
            save_state=self._save,
            commit=commit or self.commit,
            turn_kinds=lambda turn_id: kinds_of_turn(self.events, turn_id),
        )

    def _save(self, state: PersistedState) -> None:
        self.kinds_at_save.append((state.open_turn is None, self.kinds()))
        save_state(self.state_file, state)
        pending = len(list(self.inbox_dir.glob("[!.]*.json")))  # файлов ещё не подтверждено
        self.saved.put_nowait((state, pending))

    async def wait_state(self, ready: Callable[[PersistedState, int], bool]) -> None:
        async with asyncio.timeout(5):
            while True:
                if ready(*await self.saved.get()):
                    return

    def kinds(self) -> list[str]:
        return [json.loads(x)["kind"] for x in self.events.read_text().splitlines()]

    def events_of(self, kind: str) -> list[dict[str, object]]:
        rows = [json.loads(x) for x in self.events.read_text().splitlines()]
        return [r for r in rows if r["kind"] == kind]

    async def wait_kind(self, kind: EventKind) -> None:
        async with asyncio.timeout(5):
            await self.observed.seen[kind].wait()

    async def wait_count(self, kind: EventKind, count: int) -> None:
        """Ждёт `count`-е событие вида; между проверкой и `clear` нет `await`, сигнал цел."""
        async with asyncio.timeout(5):
            while len(self.events_of(kind.value)) < count:
                self.observed.seen[kind].clear()
                await self.observed.seen[kind].wait()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.log.close()


@pytest.fixture
def make_env(tmp_path: Path) -> Iterator[Callable[[PersistedState], Env]]:
    made: list[Env] = []

    def make(state: PersistedState) -> Env:
        e = Env(tmp_path, state)
        made.append(e)
        return e

    yield make
    for e in made:
        e.log.close()


async def test_turn_is_logged_and_stop_exits_zero(env: Env) -> None:
    client = FakeClient([assistant(f"key {TOKEN}"), result()])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "go"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c2", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert client.prompts == ["go"]
    assert env.kinds() == ["turn_started", "sdk", "sdk", "checkpoint", "turn_completed"]
    assert len(env.commit.turns) == 1
    assert TOKEN not in env.events.read_text()
    assert json.loads(env.health.read_text())["phase"] == "idle"
    state = load_state(env.state_file)
    assert state.open_turn is None
    assert state.last_sha == SHA
    assert state.session_id == "s1"


async def test_sdk_stream_end_is_crash(env: Env) -> None:
    client = FakeClient([])
    client.queue.put_nowait(None)
    assert await env.session.run(client) is Exit.CRASHED
    assert json.loads(env.health.read_text())["health"] == "crashed"
    assert env.events_of("turn_failed")[0]["payload"] == {"reason": "sdk_crashed"}


def hook_input(tool: str, tool_input: dict[str, object], cwd: Path) -> PreToolUseHookInput:
    return {
        "session_id": "s1",
        "transcript_path": "/x",
        "cwd": str(cwd),
        "agent_id": "a1",
        "agent_type": "general",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": "tu1",
    }


async def test_pre_tool_use_denies_and_logs(env: Env) -> None:
    ctx: HookContext = {"signal": None}
    out = await env.session.pre_tool_use(
        hook_input("Read", {"file_path": "/etc/passwd"}, env.project), "tu1", ctx
    )
    specific = out.get("hookSpecificOutput")
    assert specific is not None
    assert specific.get("permissionDecision") == "deny"
    denied = env.events_of("access_denied")
    assert denied[0]["payload"] == {"tool": "Read", "path": ETC_PASSWD, "agent_id": "a1"}


async def test_pre_tool_use_allows_inside_project(env: Env) -> None:
    ctx: HookContext = {"signal": None}
    out = await env.session.pre_tool_use(
        hook_input("Read", {"file_path": "a.md"}, env.project), "tu1", ctx
    )
    assert out == {}
    assert env.events_of("access_denied") == []


async def test_fork_waits_for_answer_from_inbox(env: Env) -> None:
    client = FakeClient([])  # ход не завершается сам: ждём развилку
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "go"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_STARTED)
    questions = [{"question": "Q?", "options": [{"label": "Да"}]}]
    ctx = ToolPermissionContext()
    ask = asyncio.create_task(
        env.session.can_use_tool("AskUserQuestion", {"questions": questions}, ctx)
    )
    await env.wait_kind(EventKind.FORK_QUESTION)
    payload = env.events_of("fork_question")[0]["payload"]
    assert isinstance(payload, dict)
    put(
        env.inbox_dir,
        {"id": "c2", "kind": "fork_answer", "fork_id": payload["fork_id"], "answers": {"Q?": "Да"}},
    )
    verdict = await ask
    assert isinstance(verdict, PermissionResultAllow)
    assert verdict.updated_input == {"questions": questions, "answers": {"Q?": "Да"}}
    client.queue.put_nowait(result())
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c3", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert "fork_answered" in env.kinds()


async def test_other_tools_are_allowed(env: Env) -> None:
    verdict = await env.session.can_use_tool("Bash", {"command": "ls"}, ToolPermissionContext())
    assert verdict == PermissionResultAllow()


async def test_open_turn_is_durable_before_query(env: Env) -> None:
    seen: list[PersistedState] = []

    class Spy(FakeClient):
        async def query(self, prompt: str) -> None:
            seen.append(load_state(env.state_file))
            await super().query(prompt)

    client = Spy([result()])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "go"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c2", "kind": "stop"})
    await run
    assert seen[0].open_turn is not None
    assert seen[0].open_turn.cmd == MessageCmd("c1", "go")


async def test_queued_command_is_durable_before_ack(env: Env) -> None:
    client = FakeClient([])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "a"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_STARTED)
    put(env.inbox_dir, {"id": "c2", "kind": "message", "text": "b"})
    observed: list[int] = []

    def has_c2(state: PersistedState, pending: int) -> bool:
        if state.queue != (MessageCmd("c2", "b"),):
            return False
        observed.append(pending)
        return True

    await env.wait_state(has_c2)
    assert observed == [1]  # файл c2 ещё в inbox в момент записи state
    put(env.inbox_dir, {"id": "c3", "kind": "stop"})
    assert await run is Exit.STOPPED


async def test_queued_turn_is_durable_before_its_query(env: Env) -> None:
    """Ход из очереди открывается после завершения предыдущего: state пишет сам `SendPrompt`."""
    seen: list[PersistedState] = []

    class Spy(FakeClient):
        async def query(self, prompt: str) -> None:
            seen.append(load_state(env.state_file))
            await super().query(prompt)

    client = Spy([])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "a"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_STARTED)
    put(env.inbox_dir, {"id": "c2", "kind": "message", "text": "b"})
    await env.wait_state(lambda state, _: state.queue == (MessageCmd("c2", "b"),))
    client.queue.put_nowait(result())  # c1 завершается, c2 берётся из очереди
    await env.wait_count(EventKind.TURN_STARTED, 2)
    put(env.inbox_dir, {"id": "c3", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert seen[1].open_turn is not None
    assert seen[1].open_turn.cmd == MessageCmd("c2", "b")
    assert seen[1].queue == ()


async def test_first_command_is_durable_before_ack(env: Env) -> None:
    """Команда, открывшая ход, попадает в state раньше, чем файл уйдёт из inbox."""
    client = FakeClient([])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "a"})
    run = asyncio.create_task(env.session.run(client))
    state, pending = await asyncio.wait_for(env.saved.get(), timeout=5)
    assert state.open_turn is not None
    assert state.open_turn.cmd == MessageCmd("c1", "a")
    assert pending == 1
    put(env.inbox_dir, {"id": "c2", "kind": "stop"})
    assert await run is Exit.STOPPED


async def test_streaming_messages_do_not_rewrite_state(env: Env) -> None:
    client = FakeClient([assistant("a"), assistant("b"), assistant("c"), result()])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "go"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c2", "kind": "stop"})
    assert await run is Exit.STOPPED
    writes = [env.saved.get_nowait() for _ in range(env.saved.qsize())]
    # открытый ход и закрытый ход: потоковые сообщения записей не добавили
    assert [(s.open_turn is not None, s.session_id) for s, _ in writes] == [
        (True, None),
        (False, "s1"),
    ]


async def test_turn_closes_in_order_commit_events_state(tmp_path: Path) -> None:
    kinds_at_commit: list[list[str]] = []
    holder: list[Env] = []

    def commit(turn_id: str) -> str:
        kinds_at_commit.append(holder[0].kinds())
        return SHA

    env = Env(tmp_path, commit=commit)
    holder.append(env)
    try:
        client = FakeClient([result()])
        put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "go"})
        run = asyncio.create_task(env.session.run(client))
        await env.wait_kind(EventKind.TURN_COMPLETED)
        put(env.inbox_dir, {"id": "c2", "kind": "stop"})
        assert await run is Exit.STOPPED
        assert kinds_at_commit == [["turn_started", "sdk"]]  # commit раньше checkpoint и completed
        closing = [kinds for closed, kinds in env.kinds_at_save if closed]
        assert closing == [
            ["turn_started", "sdk", "checkpoint", "turn_completed"]
        ]  # state последним
    finally:
        env.log.close()


class GatedCommit:
    """Commit, который держится, пока тест не отпустит его: окно `Committing` наблюдаемо."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.turns: list[str] = []

    def __call__(self, turn_id: str) -> str:
        self.turns.append(turn_id)
        self._loop.call_soon_threadsafe(self.entered.set)
        if not self.release.wait(timeout=5):
            raise TimeoutError("commit was never released")
        return SHA


async def test_command_during_commit_is_queued_and_turn_waits(tmp_path: Path) -> None:
    gate = GatedCommit(asyncio.get_running_loop())
    env = Env(tmp_path, commit=gate)
    try:
        client = FakeClient([result()])
        put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "a"})
        run = asyncio.create_task(env.session.run(client))
        async with asyncio.timeout(5):
            await gate.entered.wait()
        put(env.inbox_dir, {"id": "c2", "kind": "message", "text": "b"})
        observed: list[PersistedState] = []

        def queued(state: PersistedState, pending: int) -> bool:
            if state.queue != (MessageCmd("c2", "b"),):
                return False
            observed.append(state)
            return pending == 1  # файл c2 ещё в inbox в момент записи

        await env.wait_state(queued)
        # commit ещё идёт: ход c1 открыт, c2 ждёт, события хода не записаны, второй query не ушёл
        assert observed[0].open_turn is not None
        assert observed[0].open_turn.cmd == MessageCmd("c1", "a")
        assert client.prompts == ["a"]
        assert "checkpoint" not in env.kinds()
        assert "turn_completed" not in env.kinds()
        gate.release.set()
        await env.wait_count(EventKind.TURN_COMPLETED, 2)
        put(env.inbox_dir, {"id": "c3", "kind": "stop"})
        assert await run is Exit.STOPPED
        assert client.prompts == ["a", "b"]
        assert len(gate.turns) == 2
        assert load_state(env.state_file).queue == ()
    finally:
        gate.release.set()
        env.log.close()


async def test_commit_failure_fails_turn_without_completion(env: Env) -> None:
    env.commit.error = CheckpointError(CheckpointFailure.DISK_FULL, "no space")
    client = FakeClient([result()])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "go"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_FAILED)
    assert env.events_of("turn_failed")[0]["payload"] == {"reason": "disk_full"}
    assert "turn_completed" not in env.kinds()
    assert "checkpoint" not in env.kinds()
    put(env.inbox_dir, {"id": "c2", "kind": "stop"})  # команды принимаются дальше
    assert await run is Exit.STOPPED
    assert load_state(env.state_file).open_turn is None


def open_state(count: int = 0) -> PersistedState:
    cmd = MessageCmd("c1", "собери отчёт")
    return PersistedState("sess-1", OpenTurn("t1", cmd), None, count, ())


@pytest.mark.parametrize("mode", list(ResumeMode))
async def test_interrupted_turn_is_continued(
    make_env: Callable[[PersistedState], Env], mode: ResumeMode
) -> None:
    env = make_env(open_state(count=1))
    client = FakeClient([assistant("ok"), result()])
    step = Continue("t1", MessageCmd("c1", "собери отчёт"), 2, mode)
    run = asyncio.create_task(env.session.run(client, step))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c9", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert env.kinds()[:2] == ["turn_interrupted", "turn_resumed"]
    assert env.events_of("turn_resumed")[0]["payload"] == {"attempt": 2, "mode": mode.value}
    assert "собери отчёт" in client.prompts[0]
    assert env.kinds()[-2:] == ["checkpoint", "turn_completed"]
    state = load_state(env.state_file)
    assert state.open_turn is None
    assert state.autoresume_count == 0


async def test_attempt_is_durable_before_query(make_env: Callable[[PersistedState], Env]) -> None:
    env = make_env(open_state(count=0))
    seen: list[PersistedState] = []

    class Spy(FakeClient):
        async def query(self, prompt: str) -> None:
            seen.append(load_state(env.state_file))
            await super().query(prompt)

    client = Spy([result()])
    step = Continue("t1", MessageCmd("c1", "x"), 1, ResumeMode.RESUME)
    run = asyncio.create_task(env.session.run(client, step))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c9", "kind": "stop"})
    await run
    assert seen[0].autoresume_count == 1
    assert seen[0].open_turn is not None


async def test_attempt_counter_does_not_leak_into_next_turn(
    make_env: Callable[[PersistedState], Env],
) -> None:
    env = make_env(open_state(count=1))
    seen: list[PersistedState] = []

    class Spy(FakeClient):
        async def query(self, prompt: str) -> None:
            seen.append(load_state(env.state_file))
            await super().query(prompt)

    client = Spy([result()])
    step = Continue("t1", MessageCmd("c1", "x"), 2, ResumeMode.RESUME)
    run = asyncio.create_task(env.session.run(client, step))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c2", "kind": "message", "text": "новая задача"})
    await env.wait_count(EventKind.TURN_STARTED, 1)
    put(env.inbox_dir, {"id": "c3", "kind": "stop"})
    await run
    assert seen[0].autoresume_count == 2
    assert seen[1].open_turn is not None
    assert seen[1].open_turn.turn_id != "t1"
    assert seen[1].autoresume_count == 0  # у нового хода счёт попыток с нуля


async def test_finalize_adds_only_missing_events(make_env: Callable[[PersistedState], Env]) -> None:
    env = make_env(open_state())
    env.log.append(EventKind.TURN_STARTED, "t1", {"prompt": "x"})
    env.log.append(EventKind.CHECKPOINT, "t1", {"sha": SHA})
    client = FakeClient([])
    run = asyncio.create_task(env.session.run(client, Finalize("t1", SHA)))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c9", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert env.kinds() == ["turn_started", "checkpoint", "turn_completed"]
    assert client.prompts == []  # ход не повторяется
    state = load_state(env.state_file)
    assert state.open_turn is None
    assert state.last_sha == SHA


async def test_give_up_fails_turn_then_human_command_resumes_work(
    make_env: Callable[[PersistedState], Env],
) -> None:
    env = make_env(open_state(count=3))
    client = FakeClient([result()])
    run = asyncio.create_task(env.session.run(client, GiveUp("t1")))
    await env.wait_kind(EventKind.TURN_FAILED)
    assert env.kinds() == ["turn_interrupted", "turn_failed"]
    assert env.events_of("turn_failed")[0]["payload"] == {"reason": "resume_limit"}
    assert client.prompts == []
    put(env.inbox_dir, {"id": "c2", "kind": "message", "text": "ещё раз"})
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c3", "kind": "stop"})
    assert await run is Exit.STOPPED


async def test_redelivered_command_is_not_run_twice(
    make_env: Callable[[PersistedState], Env],
) -> None:
    env = make_env(open_state())
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "собери отчёт"})  # ack не успел
    client = FakeClient([result()])
    step = Continue("t1", MessageCmd("c1", "собери отчёт"), 1, ResumeMode.RESUME)
    run = asyncio.create_task(env.session.run(client, step))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c9", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert len(client.prompts) == 1  # только продолжение, без второго запуска c1
    assert len(env.events_of("turn_started")) == 0
    assert list(env.inbox_dir.glob("[!.]*.json")) == []  # дубль подтверждён


async def test_redelivered_queued_command_is_acked_without_requeue(
    make_env: Callable[[PersistedState], Env],
) -> None:
    queued = MessageCmd("c2", "потом")
    state = PersistedState(None, None, None, 0, (queued,))
    env = make_env(state)
    put(env.inbox_dir, {"id": "c2", "kind": "message", "text": "потом"})  # ack не успел
    client = FakeClient([result()])
    run = asyncio.create_task(env.session.run(client))  # Clean: очередь стартует
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c9", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert client.prompts == ["потом"]  # один раз: из очереди, не из повторной доставки
