import os
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from evals.conftest import Project, docker
from evals.fake_gateway import ThreadedGateway

GW_TOKEN = "gw-e2e-secret-token"  # noqa: S105 — тестовый токен фейкового gateway
PROMPT = (  # формулировка проверена спайком (Findings дизайна)
    "Сначала задай мне вопрос инструментом AskUserQuestion: «Какой формат?» с вариантами md и txt. "
    "После моего ответа создай файл report.<формат> с текстом ok."
)


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[ThreadedGateway]:
    gw = ThreadedGateway(host=os.environ.get("FAKE_GATEWAY_BIND", "127.0.0.1"), token=GW_TOKEN)
    monkeypatch.setenv("GATEWAY_URL", gw.url)  # sessions подменит localhost на host.docker.internal
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", GW_TOKEN)
    yield gw
    gw.close()


def start_run(gw: ThreadedGateway) -> str:
    run_id = uuid.uuid4().hex
    gw.send({"v": 1, "type": "command", "id": run_id, "kind": "message", "text": PROMPT})
    return run_id


def is_event(run_id: str, kind: str) -> Callable[[dict[str, Any]], bool]:
    return lambda m: m.get("event", {}).get("run_id") == run_id and m["event"]["type"] == kind


@pytest.mark.e2e
def test_question_answer_continues_same_run(
    gateway: ThreadedGateway, start_project: Callable[..., Project]
) -> None:
    project = start_project()
    hello = gateway.wait_for(lambda m: m.get("type") == "hello", 120)
    assert hello["project_id"] == project.id
    run_id = start_run(gateway)
    question = gateway.wait_for(is_event(run_id, "facilitator.question"))["event"]["payload"]
    text = question["questions"][0]["text"]

    gateway.send({"v": 1, "type": "command", "id": uuid.uuid4().hex, "kind": "fork_answer",
                  "fork_id": "stale-fork", "answers": {text: "md"}})  # fmt: skip
    stale = gateway.wait_for(
        lambda m: m.get("type") == "command_ack" and m.get("reason") == "unknown_fork"
    )
    assert stale["status"] == "rejected"

    answer_id = uuid.uuid4().hex
    gateway.send({"v": 1, "type": "command", "id": answer_id, "kind": "fork_answer",
                  "fork_id": question["question_id"], "answers": {text: "md"}})  # fmt: skip
    assert gateway.wait_for(lambda m: m.get("id") == answer_id)["status"] == "accepted"
    gateway.wait_for(is_event(run_id, "run.completed"), 300)

    types = [e["type"] for e in gateway.events(run_id)]
    assert types.count("run.started") == 1  # тот же Run, нового хода нет
    assert types.index("facilitator.question") < types.index("user.answer.accepted")
    assert types.index("user.answer.accepted") < types.index("run.completed")
    assert any(e["type"] == "artifact.created" and e["payload"]["path"].startswith("report.")
               for e in gateway.events(run_id))  # fmt: skip


@pytest.mark.e2e
def test_restart_while_waiting_reasks_and_finishes(
    gateway: ThreadedGateway, start_project: Callable[..., Project]
) -> None:
    project = start_project()
    gateway.wait_for(lambda m: m.get("type") == "hello", 120)
    run_id = start_run(gateway)
    first = gateway.wait_for(is_event(run_id, "facilitator.question"))["event"]["payload"]
    docker("kill", project.container)
    docker("start", project.container)  # ручной kill: политика on-failure контейнер не поднимет
    second = gateway.wait_for(
        lambda m: (
            is_event(run_id, "facilitator.question")(m)
            and m["event"]["payload"]["question_id"] != first["question_id"]
        ),
        240,
    )["event"]["payload"]
    text = second["questions"][0]["text"]
    answer_id = uuid.uuid4().hex
    gateway.send({"v": 1, "type": "command", "id": answer_id, "kind": "fork_answer",
                  "fork_id": second["question_id"], "answers": {text: "md"}})  # fmt: skip
    gateway.wait_for(is_event(run_id, "run.completed"), 300)
    types = [e["type"] for e in gateway.events(run_id)]
    assert types.count("run.started") == 1
    assert "recovering" in [e["payload"].get("stage") for e in gateway.events(run_id)
                            if e["type"] == "progress.updated"]  # fmt: skip
