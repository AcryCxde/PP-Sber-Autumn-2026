"""Машина состояний хода: (состояние, вход) → (новое состояние, эффекты). Без ввода-вывода."""

from dataclasses import dataclass, replace
from typing import assert_never, final

from claude_agent_sdk import Message, ResultMessage, SystemMessage

from ctrunner.liveness import Phase
from ctrunner.protocol import MessageCmd


@final
@dataclass(frozen=True, slots=True)
class Idle:
    queue: tuple[MessageCmd, ...]

    @property
    def phase(self) -> Phase:
        return Phase.IDLE


@final
@dataclass(frozen=True, slots=True)
class Working:
    turn_id: str
    cmd: MessageCmd  # команда, открывшая ход: нужна, чтобы повторить его после рестарта
    pending: frozenset[str]  # фоновые задачи: пока они есть, `result` не завершает ход
    forks: int  # открытые развилки AskUserQuestion
    queue: tuple[MessageCmd, ...]

    def __post_init__(self) -> None:
        if self.forks < 0:
            raise ValueError("forks must be non-negative")

    @property
    def phase(self) -> Phase:
        return Phase.AWAITING_ANSWER if self.forks else Phase.WORKING


@final
@dataclass(frozen=True, slots=True)
class Committing:
    """Результат получен, идёт git commit. Новые команды ждут в очереди."""

    turn_id: str
    cmd: MessageCmd
    queue: tuple[MessageCmd, ...]

    @property
    def phase(self) -> Phase:
        return Phase.WORKING


@final
@dataclass(frozen=True, slots=True)
class Halted:
    """Автопродолжения исчерпаны: runner жив, `health=crashed`, ждёт команду человека."""

    queue: tuple[MessageCmd, ...]

    @property
    def phase(self) -> Phase:
        return Phase.FAILED


type TurnState = Idle | Working | Committing | Halted


@final
@dataclass(frozen=True, slots=True)
class SendPrompt:
    turn_id: str
    text: str


@final
@dataclass(frozen=True, slots=True)
class Commit:
    turn_id: str


@final
@dataclass(frozen=True, slots=True)
class TurnDone:
    turn_id: str


@final
@dataclass(frozen=True, slots=True)
class TurnFailedFx:
    turn_id: str
    reason: str


type Effect = SendPrompt | Commit | TurnDone | TurnFailedFx
type Step = tuple[TurnState, tuple[Effect, ...]]


def on_command(s: TurnState, cmd: MessageCmd, *, turn_id: str) -> Step:
    match s:
        case Idle():
            return next_queued(Idle((*s.queue, cmd)), turn_id=turn_id)
        case Halted():  # команда человека возвращает runner в работу
            return next_queued(Idle((*s.queue, cmd)), turn_id=turn_id)
        case Working() | Committing():
            return replace(s, queue=(*s.queue, cmd)), ()
        case _:
            assert_never(s)


def next_queued(s: Idle, *, turn_id: str) -> Step:
    if not s.queue:
        return s, ()
    first, *rest = s.queue
    return (
        Working(turn_id, first, frozenset(), 0, tuple(rest)),
        (SendPrompt(turn_id, first.text),),
    )


def on_message(s: TurnState, msg: Message) -> Step:
    match s:
        case Idle() | Halted() | Committing():
            # `result` фоновой доработки после хода или запоздавшее сообщение: журнал уже записан.
            return s, ()
        case Working():
            return _working_on_message(s, msg)
        case _:
            assert_never(s)


def _working_on_message(s: Working, msg: Message) -> Step:
    if isinstance(msg, SystemMessage):
        return replace(s, pending=_track_background(s.pending, msg)), ()
    if not isinstance(msg, ResultMessage):
        return s, ()
    if msg.is_error:
        return Idle(s.queue), (TurnFailedFx(s.turn_id, "result_error"),)
    if s.pending:
        return s, ()
    return Committing(s.turn_id, s.cmd, s.queue), (Commit(s.turn_id),)


def on_committed(s: Committing) -> Step:
    return Idle(s.queue), (TurnDone(s.turn_id),)


def on_commit_failed(s: Committing, reason: str) -> Step:
    return Idle(s.queue), (TurnFailedFx(s.turn_id, reason),)


def _track_background(pending: frozenset[str], msg: SystemMessage) -> frozenset[str]:
    task_id = msg.data.get("task_id")
    match msg.subtype:
        case "task_started" if isinstance(task_id, str) and _is_backgrounded(msg):
            return pending | {task_id}
        case "task_notification" if isinstance(task_id, str):
            return pending - {task_id}
        case "background_tasks_changed" if isinstance(tasks := msg.data.get("tasks"), list):
            return frozenset(
                t["task_id"]
                for t in tasks
                if isinstance(t, dict) and isinstance(t.get("task_id"), str)
            )
        case _:
            # subtype — открытое множество строк SDK, не наш закрытый тип.
            return pending


def _is_backgrounded(msg: SystemMessage) -> bool:
    # SDK присылает флаг то булевым, то строкой (замечено в песочнице).
    return msg.data.get("is_backgrounded") in (True, "True")


def on_fork_open(s: Working) -> Working:
    return replace(s, forks=s.forks + 1)


def on_fork_closed(s: Working) -> Working:
    return replace(s, forks=s.forks - 1)
