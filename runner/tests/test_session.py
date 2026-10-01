import asyncio
import json
import os
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator, Sequence
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

from ctrunner.eventlog import Event, EventLog
from ctrunner.guard import Policy
from ctrunner.inbox import Inbox, put
from ctrunner.protocol import EventKind, JsonValue
from ctrunner.redact import Redactor
from ctrunner.session import Exit, Session

TOKEN = "sk-test-0123456789"
ETC_PASSWD = os.path.realpath("/etc/passwd")  # на macOS это /private/etc/passwd


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
    def __init__(self, root: Path) -> None:
        self.inbox_dir = root / "inbox"
        self.events = root / "events.jsonl"
        self.health = root / "health.json"
        self.project = root / "project"
        self.project.mkdir()
        redactor = Redactor.of([TOKEN])
        self.log = EventLog.open(self.events, redactor, time.time)
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
        )

    def kinds(self) -> list[str]:
        return [json.loads(x)["kind"] for x in self.events.read_text().splitlines()]

    def events_of(self, kind: str) -> list[dict[str, object]]:
        rows = [json.loads(x) for x in self.events.read_text().splitlines()]
        return [r for r in rows if r["kind"] == kind]

    async def wait_kind(self, kind: EventKind) -> None:
        async with asyncio.timeout(5):
            await self.observed.seen[kind].wait()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.log.close()


async def test_turn_is_logged_and_stop_exits_zero(env: Env) -> None:
    client = FakeClient([assistant(f"key {TOKEN}"), result()])
    put(env.inbox_dir, {"id": "c1", "kind": "message", "text": "go"})
    run = asyncio.create_task(env.session.run(client))
    await env.wait_kind(EventKind.TURN_COMPLETED)
    put(env.inbox_dir, {"id": "c2", "kind": "stop"})
    assert await run is Exit.STOPPED
    assert client.prompts == ["go"]
    assert env.kinds() == ["turn_started", "sdk", "sdk", "turn_completed"]
    assert TOKEN not in env.events.read_text()
    assert json.loads(env.health.read_text())["phase"] == "idle"


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
