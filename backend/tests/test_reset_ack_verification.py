"""PR36 reset/COMMIT-ACK recovery and executor-context regressions."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from backend import main
from backend.models import Conversation, Event, Run
from backend.tests.auth_helpers import sign_in
from backend.tests.test_history_api import FakeProcess, claude_lines, make_app
from backend.tests.test_runner_executor import (
    FakeDocker, FakeRunner, make_runner_app, receive_until_terminal, run_command,
)


@pytest.mark.parametrize("old_waiting", [False, True], ids=["old-idle", "old-waiting"])
@pytest.mark.parametrize("outcome", ["success", "rollback_ack_lost", "commit_ack_lost"])
def test_explicit_reset_must_not_reuse_old_runtime(tmp_path, monkeypatch, old_waiting, outcome):
    docker = FakeDocker()
    observations = []
    question = {"text": "OLD_QUESTION", "header": None, "multi_select": False,
                "allow_custom": True, "options": [{"label": "old", "description": None}]}

    def script(command, container):
        if command.get("text") == "SEED_OLD_CONTEXT":
            container.sdk_context = "OLD_CONTEXT"
            if old_waiting:
                return [("run.started", {}), ("facilitator.question", {
                    "question_id": "OLD_QUESTION_ID", "questions": [question]})]
        marker = getattr(container, "sdk_context", "NEW_CONTEXT")
        observations.append((command["kind"], container.id, marker))
        result = [("message.created", {"role_id": None, "text": marker}), ("run.completed", {})]
        if command["kind"] == "message":
            return [("run.started", {})] + result
        return [("user.answer.accepted", {"question_id": "OLD_QUESTION_ID",
                                           "answers": command["answers"]})] + result

    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        sign_in(client)
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as socket:
                socket.send_json(run_command("SEED_OLD_CONTEXT"))
                first = receive_until_terminal(socket)
                conversation_id = first[0]["conversation_id"]
                project = conversation_id.lower()
                old_container = docker.containers.get(f"ct-{project}")
                assert first[-1]["status"] == "completed"
                if old_waiting:
                    assert app.state.runner_transport.active_run(project)["status"] == "waiting_for_input"
                else:
                    assert observations[-1][2] == "OLD_CONTEXT"

                real_transaction = main.policy_transaction
                commit_seen = []

                async def inject_ack_loss(session, operation):
                    real_commit, real_rollback = session.commit, session.rollback

                    async def lose_ack():
                        if outcome == "commit_ack_lost":
                            await real_commit()
                        else:
                            await real_rollback()
                        async with app.state.database.session() as fresh:
                            conversation = await fresh.get(Conversation, conversation_id)
                            reset_run = await fresh.scalar(select(Run).where(
                                Run.conversation_id == conversation_id, Run.context_mode == "reset"))
                            commit_seen.append((reset_run.status, reset_run.admitted_at is not None,
                                                conversation.claude_session_id, conversation.context_state))
                        raise DBAPIError("COMMIT", None, ConnectionError("deterministic lost ACK"),
                                         connection_invalidated=True)

                    monkeypatch.setattr(session, "commit", lose_ack)
                    return await real_transaction(session, operation)

                before_reset_commands = len(runner.received)
                if outcome != "success":
                    monkeypatch.setattr(main, "policy_transaction", inject_ack_loss)
                reset_command = run_command("EXPLICIT_RESET", conversation_id)
                reset_command["context_mode"] = "reset"
                socket.send_json(reset_command)
                reset_events = receive_until_terminal(socket)
                monkeypatch.setattr(main, "policy_transaction", real_transaction)
                if outcome != "success":
                    assert reset_events[-1]["error_code"] == "ai_policy_unavailable"
                    assert len(runner.received) == before_reset_commands
                    assert not old_container.removed
                    expected_durable = (("running", True, None, "reset") if outcome == "commit_ack_lost"
                                        else ("pending", False, f"runner-{project}", "active"))
                    assert commit_seen == [expected_durable]
                else:
                    assert reset_events[-1]["status"] == "completed"
                    assert old_container.removed

                async def after_recovery():
                    async with app.state.database.session() as session:
                        conversation = await session.get(Conversation, conversation_id)
                        reset_run = await session.scalar(select(Run).where(
                            Run.conversation_id == conversation_id, Run.context_mode == "reset"))
                        events = list(await session.scalars(select(Event.type).where(
                            Event.run_id == reset_run.id).order_by(Event.seq)))
                        return (conversation.claude_session_id, conversation.context_state, events)

                persisted = client.portal.call(after_recovery)
                socket.send_json(run_command("MUST_USE_NEW_CONTEXT", conversation_id))
                if outcome == "commit_ack_lost":
                    refused = socket.receive_json()
                    assert refused["type"] == "command.error"
                    assert refused["code"] == "conversation_context_unavailable"
                    assert len(runner.received) == before_reset_commands
                    assert persisted == (None, "unavailable", [
                        "run.accepted", "context.reset", "run.started", "run.failed"])
                    assert reset_events[-1]["session_state"] == "unavailable"
                    assert reset_events[-1]["can_reset_context"] is True
                    # A later explicit reset, not this recovery attempt, owns discard.
                    retry = run_command("MUST_USE_NEW_CONTEXT", conversation_id)
                    retry["context_mode"] = "reset"
                    socket.send_json(retry)
                following = receive_until_terminal(socket)
                assert following[-1]["status"] == "completed"
                actual = [event["content"] for event in following if event["type"] == "message.created"]
                accepted_mode = following[0]["context_mode"]
                expected = "OLD_CONTEXT" if outcome == "rollback_ack_lost" else "NEW_CONTEXT"
                evidence = {"outcome": outcome, "old_waiting": old_waiting,
                            "durable_before_ACK_loss": commit_seen, "after_recovery": persisted,
                            "following_context_mode": accepted_mode, "following_reply": actual,
                            "following_runtime": observations[-1], "old_container": old_container.id,
                            "old_removed": old_container.removed, "runner_commands": runner.received}
                assert actual == [expected], evidence
                if expected == "NEW_CONTEXT":
                    assert observations[-1][0] == "message"
                    assert observations[-1][1] != old_container.id
                else:
                    assert observations[-1][1] == old_container.id
        finally:
            runner.halt.set()
            runner.join(timeout=3)
            assert not runner.is_alive()


@pytest.mark.parametrize("applied", [False, True], ids=["rollback", "committed"])
def test_cli_reset_ack_loss_preserves_existing_semantics(tmp_path, monkeypatch, applied):
    calls = []

    async def cli(prompt, workspace, resume_session_id=None, *, permission_mode=None):
        calls.append(resume_session_id)
        return FakeProcess(claude_lines(
            "OLD_CONTEXT" if resume_session_id == "OLD_SESSION" else "NEW_CONTEXT",
            session_id="OLD_SESSION" if prompt == "SEED_OLD_CONTEXT" or resume_session_id else "NEW_SESSION"))

    monkeypatch.setattr(main, "start_claude", cli)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        with client.websocket_connect("/ws") as socket:
            socket.send_json(run_command("SEED_OLD_CONTEXT"))
            first = receive_until_terminal(socket)
            conversation_id = first[0]["conversation_id"]
            real_transaction = main.policy_transaction

            async def uncertain(session, operation):
                real_commit, real_rollback = session.commit, session.rollback

                async def lose_ack():
                    if applied:
                        await real_commit()
                    else:
                        await real_rollback()
                    raise DBAPIError("COMMIT", None, ConnectionError("lost ACK"),
                                     connection_invalidated=True)

                monkeypatch.setattr(session, "commit", lose_ack)
                return await real_transaction(session, operation)

            monkeypatch.setattr(main, "policy_transaction", uncertain)
            reset = run_command("RESET", conversation_id)
            reset["context_mode"] = "reset"
            socket.send_json(reset)
            failed = receive_until_terminal(socket)
            assert failed[-1]["error_code"] == "ai_policy_unavailable"
            assert failed[-1]["session_state"] == ("reset" if applied else "active")
            assert calls == [None]
            monkeypatch.setattr(main, "policy_transaction", real_transaction)
            socket.send_json(run_command("FOLLOWING", conversation_id))
            following = receive_until_terminal(socket)
            assert following[0]["context_mode"] == ("new" if applied else "resume")
            assert calls == [None, None if applied else "OLD_SESSION"]
            assert [event["content"] for event in following if event["type"] == "message.created"] == [
                "NEW_CONTEXT" if applied else "OLD_CONTEXT"]
