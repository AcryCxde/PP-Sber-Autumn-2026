import asyncio
import json
from pathlib import Path
from typing import Any, Self

from ctrunner.eventlog import EventLog
from ctrunner.gateway import GatewayClient
from ctrunner.inbox import Inbox
from ctrunner.protocol import EventKind, JsonValue, MessageCmd
from ctrunner.redact import Redactor
from evals.fake_gateway import FakeGateway

TOKEN = "gw-secret-token"
QUESTIONS: list[JsonValue] = [{"question": "Q?", "options": [{"label": "A"}]}]


class Rig:
    """Клиент, журнал и inbox во временном каталоге; клиент работает, пока открыт `async with`."""

    def __init__(
        self, root: Path, gateway: FakeGateway, token: str = TOKEN, poll_s: float = 0.02
    ) -> None:
        self.journal = root / "events.jsonl"
        self.health = root / "health.json"
        self.inbox = root / "inbox"
        self.log = EventLog.open(self.journal, Redactor.of([]), lambda: 1791273600.0)
        self.forks: dict[str, tuple[str, ...]] = {}
        self.client = GatewayClient(
            url=gateway.url, token=token, project_id="demo-a", runner_version="0.1.0",
            session_id="s-1", journal=self.journal, health_file=self.health,
            inbox_dir=self.inbox, project_root="/workspace/project",
            last_seq=lambda: self.log.last_seq, fork_questions=self.forks.get,
            poll_s=poll_s, heartbeat_s=0.05, backoff=(0.05,),
        )  # fmt: skip
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self.task = asyncio.create_task(self.client.run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.task is not None
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.log.close()


def is_type(kind: str) -> Any:
    return lambda m: m.get("type") == kind


async def test_hello_then_events_after_welcome(tmp_path: Path) -> None:
    async with FakeGateway(token=TOKEN) as gw:
        rig = Rig(tmp_path, gw)
        rig.log.append(EventKind.SESSION_STARTED, None, {})
        rig.log.append(EventKind.TURN_STARTED, "run-1", {"prompt": "go"})
        rig.log.append(EventKind.FORK_QUESTION, "run-1", {"fork_id": "f1", "questions": QUESTIONS})
        async with rig:
            hello = await gw.wait_for(is_type("hello"))
            assert hello == {
                "v": 1, "type": "hello", "project_id": "demo-a", "runner_version": "0.1.0",
                "session_id": "s-1", "last_seq": 3,
            }  # fmt: skip
            await gw.wait_for(lambda m: m.get("event", {}).get("type") == "facilitator.question")
        assert [e["event"]["type"] for e in gw.events()] == ["run.started", "facilitator.question"]
        assert [e["journal_seq"] for e in gw.events()] == [2, 3]
        assert {e["event"]["run_id"] for e in gw.events()} == {"run-1"}


async def test_events_before_acked_seq_are_not_resent(tmp_path: Path) -> None:
    async with FakeGateway(token=TOKEN, acked_seq=2) as gw:
        rig = Rig(tmp_path, gw)
        rig.log.append(EventKind.TURN_STARTED, "run-1", {"prompt": "go"})
        rig.log.append(EventKind.FORK_QUESTION, "run-1", {"fork_id": "f1", "questions": QUESTIONS})
        rig.log.append(EventKind.FORK_ANSWERED, "run-1", {"fork_id": "f1", "answers": {"Q?": "A"}})
        async with rig:
            await gw.wait_for(lambda m: m.get("event", {}).get("type") == "user.answer.accepted")
        assert [e["journal_seq"] for e in gw.events()] == [2, 3]  # строка acked_seq досылается


async def test_commands_are_acked_and_land_in_inbox(tmp_path: Path) -> None:
    async with FakeGateway(token=TOKEN) as gw:
        rig = Rig(tmp_path, gw)
        rig.forks["f1"] = ("Q?",)
        async with rig:
            await gw.wait_for(is_type("hello"))
            await gw.send(
                {"v": 1, "type": "command", "id": "run-9", "kind": "message", "text": "hi"}
            )
            ok = await gw.wait_for(lambda m: m.get("id") == "run-9")
            await gw.send({"v": 1, "type": "command", "id": "a1", "kind": "fork_answer",
                           "fork_id": "f9", "answers": {"Q?": "A"}})  # fmt: skip
            stale = await gw.wait_for(lambda m: m.get("id") == "a1")
            await gw.send({"v": 1, "type": "command", "id": "a2", "kind": "fork_answer",
                           "fork_id": "f1", "answers": {"Q?": "A"}})  # fmt: skip
            good = await gw.wait_for(lambda m: m.get("id") == "a2")
        assert (ok["status"], stale["status"], stale["reason"], good["status"]) == (
            "accepted", "rejected", "unknown_fork", "accepted",
        )  # fmt: skip
        commands = [d.command for d in Inbox(rig.inbox).peek()]
        assert MessageCmd("run-9", "hi") in commands
        assert len(commands) == 2


async def test_heartbeat_mirrors_health_file(tmp_path: Path) -> None:
    async with FakeGateway(token=TOKEN) as gw:
        rig = Rig(tmp_path, gw)
        rig.health.write_text(json.dumps({"phase": "awaiting_answer", "health": "ok",
                                          "last_progress_at": 5.0, "written_at": 6.0}))  # fmt: skip
        async with rig:
            beat = await gw.wait_for(is_type("heartbeat"))
        assert beat["phase"] == "awaiting_answer"
        assert beat["health"] == "ok"
        assert beat["turn_id"] is None


async def test_reconnect_continues_and_resends_only_by_event_id(tmp_path: Path) -> None:
    async with FakeGateway(token=TOKEN) as gw:
        rig = Rig(tmp_path, gw)
        rig.log.append(EventKind.TURN_STARTED, "run-1", {"prompt": "go"})
        async with rig:
            await gw.wait_for(lambda m: m.get("event", {}).get("type") == "run.started")
            await gw.drop()
            rig.log.append(
                EventKind.FORK_QUESTION, "run-1", {"fork_id": "f1", "questions": QUESTIONS}
            )
            await gw.wait_for(lambda m: m.get("event", {}).get("type") == "facilitator.question")
        assert gw.connections == 2
        events = gw.events()
        assert {e["event"]["type"] for e in events} == {"run.started", "facilitator.question"}
        # строка acked_seq уходит повторно с тем же event_id — gateway по нему отсекает дубль
        started = [e["event"]["event_id"] for e in events if e["event"]["type"] == "run.started"]
        assert len(set(started)) == 1


async def test_wrong_token_never_crashes_client(tmp_path: Path) -> None:
    async with FakeGateway(token="other-token") as gw:
        rig = Rig(tmp_path, gw)
        async with rig:
            await asyncio.sleep(0.3)
            assert rig.task is not None
            assert not rig.task.done()
        assert gw.received == []


async def test_drain_sends_tail_written_just_before_exit(tmp_path: Path) -> None:
    async with FakeGateway(token=TOKEN) as gw:
        rig = Rig(tmp_path, gw, poll_s=30.0)  # без drain хвост не ушёл бы за время теста
        async with rig:
            await gw.wait_for(is_type("hello"))
            rig.log.append(EventKind.TURN_STARTED, "run-1", {"prompt": "go"})
            rig.client.drain()
            assert rig.task is not None
            await asyncio.wait_for(rig.task, timeout=5)
        assert [e["event"]["type"] for e in gw.events()] == ["run.started"]
