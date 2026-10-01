import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from claude_agent_sdk import ProcessError

from ctrunner.checkpoint import Head
from ctrunner.eventlog import EventLog
from ctrunner.main import EX_STATE, halt_on_corrupt_state, resumable_session, run_with_resume
from ctrunner.protocol import EventKind, MessageCmd
from ctrunner.recovery import CLEAN, Continue, Recovery, ResumeMode
from ctrunner.redact import Redactor
from ctrunner.session import SdkClient
from ctrunner.state import EMPTY, OpenTurn, PersistedState, StateCorrupt

SID = "749df2fe-1b50-4c46-92f3-e2cdf0ce322d"
HEAD = Head("a" * 40, None)
OPEN = PersistedState(SID, OpenTurn("t1", MessageCmd("c1", "go")), None, 0, ())


def with_session(session_id: str | None) -> PersistedState:
    return PersistedState(session_id, None, None, 0, ())


def test_resumable_when_transcript_exists(tmp_path: Path) -> None:
    transcript = tmp_path / "projects" / "-workspace-project" / f"{SID}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    assert resumable_session(with_session(SID), tmp_path) == SID


def test_not_resumable_without_transcript_or_session(tmp_path: Path) -> None:
    assert resumable_session(with_session(SID), tmp_path) is None
    assert resumable_session(EMPTY, tmp_path) is None


def test_corrupt_state_is_logged_and_exits_with_state_code(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with EventLog.open(path, Redactor.of([]), lambda: 1.0) as log:
        assert halt_on_corrupt_state(log, StateCorrupt("bad")) == EX_STATE
    row = json.loads(path.read_text().splitlines()[0])
    assert row["kind"] == EventKind.TURN_FAILED
    assert row["payload"] == {"reason": "state_corrupt"}


@dataclass
class FakeSession:
    """Вместо Session: запоминает, с каким состоянием собран и что ему велели выполнить."""

    state: PersistedState
    recovery: Recovery | None = None
    crashed: BaseException | None = None
    run_error: BaseException | None = None

    async def run(self, client: SdkClient, recovery: Recovery = CLEAN) -> int:
        self.recovery = recovery
        if self.run_error is not None:
            raise self.run_error
        return 0

    def crash(self, error: BaseException | None) -> int:
        self.crashed = error
        return 1


@dataclass
class Harness:
    """Собирает FakeSession и «клиента SDK»: вход с resume из `connect_fails` падает."""

    connect_fails: set[str | None] = field(default_factory=set)
    connect_error: BaseException | None = None
    run_error: BaseException | None = None
    built: list[FakeSession] = field(default_factory=list)
    opened: list[str | None] = field(default_factory=list)

    def build(self, state: PersistedState) -> FakeSession:
        session = FakeSession(state, run_error=self.run_error)
        self.built.append(session)
        return session

    @asynccontextmanager
    async def open(self, session: FakeSession, resume: str | None) -> AsyncIterator[SdkClient]:
        self.opened.append(resume)
        if resume in self.connect_fails:
            raise self.connect_error or ProcessError("No conversation found", exit_code=1)
        yield object()  # type: ignore[misc]  # клиент нужен только как значение


async def drive(harness: Harness, state: PersistedState, *, resumable: str | None) -> int:
    return await run_with_resume(
        state, HEAD, resumable, build_session=harness.build, open_client=harness.open
    )


async def test_resume_is_used_when_it_connects() -> None:
    harness = Harness()
    assert await drive(harness, OPEN, resumable=SID) == 0
    assert harness.opened == [SID]
    assert harness.built[0].state.session_id == SID
    assert harness.built[0].recovery == Continue("t1", OPEN.open_turn.cmd, 1, ResumeMode.RESUME)  # type: ignore[union-attr]


async def test_failed_resume_falls_back_to_fresh_with_rebuilt_session() -> None:
    harness = Harness(connect_fails={SID})
    assert await drive(harness, OPEN, resumable=SID) == 0
    assert harness.opened == [SID, None]
    first, second = harness.built
    assert first is not second
    assert first.recovery is None  # старая сессия не запускалась
    assert second.state == PersistedState(None, OPEN.open_turn, None, 0, ())
    assert second.recovery == Continue("t1", OPEN.open_turn.cmd, 1, ResumeMode.FRESH)  # type: ignore[union-attr]


async def test_second_connect_failure_is_a_crash_without_more_retries() -> None:
    harness = Harness(connect_fails={SID, None})
    assert await drive(harness, OPEN, resumable=SID) == 1
    assert harness.opened == [SID, None]
    assert isinstance(harness.built[-1].crashed, ProcessError)


async def test_connect_failure_without_resume_is_a_crash() -> None:
    harness = Harness(connect_fails={None})
    assert await drive(harness, EMPTY, resumable=None) == 1
    assert harness.opened == [None]


async def test_process_error_after_connect_is_not_a_resume_failure() -> None:
    error = ProcessError("cli died", exit_code=-9)
    harness = Harness(run_error=error)
    assert await drive(harness, OPEN, resumable=SID) == 1
    assert harness.opened == [SID]
    assert harness.built[0].crashed is error


async def test_other_connect_error_is_a_crash_not_a_fallback() -> None:
    harness = Harness(connect_fails={SID}, connect_error=OSError("no cli"))
    assert await drive(harness, OPEN, resumable=SID) == 1
    assert harness.opened == [SID]
    assert isinstance(harness.built[0].crashed, OSError)


@pytest.mark.parametrize("state", [EMPTY, with_session(SID)])
async def test_clean_state_plans_nothing(state: PersistedState) -> None:
    harness = Harness()
    await drive(harness, state, resumable=None)
    assert harness.built[0].recovery == CLEAN
