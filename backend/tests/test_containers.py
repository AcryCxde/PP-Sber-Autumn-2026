"""ContainerManager: восстановление токенов, наблюдение, уборка контейнеров и срок хранения томов."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.containers import ContainerManager, _docker_time
from backend.runner_app import RunnerBridge, create_runner_app
from backend.runner_transport import RunnerTransport
from backend.tests.fake_docker import FakeDocker, unpack


VOLUMES = {"proj-p1", "ctsec-p1", "ctseed-p1"}


def make_manager(tmp_path, docker, transport=None, **overrides):
    transport = transport or RunnerTransport(tmp_path / "transport.db")
    context = tmp_path / "context"
    context.mkdir(exist_ok=True)
    (context / "README.md").write_text("demo", encoding="utf-8")
    settings = Settings(
        anthropic_auth_token="test-anthropic-token-0123456789",
        runner_secret_file=tmp_path / "gateway.secret",
        runner_context_dir=context,
        runner_idle_timeout_seconds=600,
        runner_connect_timeout_seconds=180,
        runner_volume_retention_seconds=overrides.pop("retention", 3600),
        **overrides,
    )
    return ContainerManager(settings, RunnerBridge(transport), transport, client=docker, environ={})


@pytest.fixture
def setup(tmp_path):
    docker = FakeDocker()
    manager = make_manager(tmp_path, docker)
    return manager, manager.transport, docker


def test_tokens_are_stable_per_project_and_restored_after_restart(setup, tmp_path):
    manager, transport, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    assert manager.token("p1") != manager.token("p2")
    again = make_manager(tmp_path, docker, transport)
    assert again.token("p1") == manager.token("p1")
    assert asyncio.run(again.restore()) == ["p1"]
    assert again.bridge._tokens == {"p1": manager.token("p1")}
    assert again.last_activity("p1") == manager.last_activity("p1")


def test_idle_container_is_removed_but_volumes_are_kept(setup):
    manager, transport, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    now = time.time()
    asyncio.run(manager.supervise_once(now=now + 599))
    assert docker.removed_containers == []
    asyncio.run(manager.supervise_once(now=now + 601))
    assert docker.stopped == ["ct-p1"] and docker.removed_containers == ["ct-p1"]
    assert set(docker.volumes._items) == VOLUMES
    assert "p1" not in manager.bridge._tokens
    status = manager.describe("p1")
    assert status["state"] == "missing" and status["volumes_kept"] is True


def test_next_message_recreates_container_from_kept_volumes(setup):
    manager, transport, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    first = docker.containers.get("ct-p1")
    asyncio.run(manager.retire("p1"))
    assert asyncio.run(manager.ensure_running("p1")) is True
    second = docker.containers.get("ct-p1")
    assert second is not first
    assert second.kwargs["volumes"] == first.kwargs["volumes"]
    # Тома не пересоздавались, контекст скопирован один раз, секреты переписаны заново.
    assert sorted(docker.created_volumes) == sorted(VOLUMES)
    archives = [path for helper in docker.containers.helpers for path, _ in helper.archives]
    assert archives == ["/secrets", "/seed", "/secrets"]
    secrets = unpack(docker.containers.helpers[1].archives[0][1])
    assert secrets["gateway_token"][0].decode().strip() == manager.token("p1")
    assert manager.bridge._tokens == {"p1": manager.token("p1")}


def test_stopped_container_is_recreated_on_current_image(setup):
    manager, transport, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    first = docker.containers.get("ct-p1")
    first.exit(1)
    asyncio.run(manager.ensure_running("p1"))
    assert first.removed and docker.containers.get("ct-p1") is not first


def test_volumes_are_removed_after_retention_from_last_activity(setup):
    manager, transport, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    now = time.time()
    asyncio.run(manager.supervise_once(now=now + 601))
    asyncio.run(manager.supervise_once(now=now + 3599))
    assert set(docker.volumes._items) == VOLUMES
    asyncio.run(manager.supervise_once(now=now + 3601))
    assert docker.volumes._items == {}
    assert manager.last_activity("p1") is None


def test_zero_retention_keeps_volumes_forever(tmp_path):
    docker = FakeDocker()
    manager = make_manager(tmp_path, docker, retention=0)
    asyncio.run(manager.ensure_running("p1"))
    asyncio.run(manager.supervise_once(now=time.time() + 10 * 365 * 86400))
    assert docker.removed_containers == ["ct-p1"]
    assert set(docker.volumes._items) == VOLUMES


def test_retention_counts_from_persisted_activity_after_backend_restart(setup, tmp_path):
    manager, transport, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    asyncio.run(manager.retire("p1"))
    restarted = make_manager(tmp_path, docker, transport)
    asyncio.run(restarted.restore())
    asyncio.run(restarted.supervise_once(now=manager.last_activity("p1") + 3601))
    assert docker.volumes._items == {}


def test_discard_closes_active_run_and_removes_everything(setup):
    manager, transport, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    transport.message("p1", "run-1", "c1", "go")
    asyncio.run(manager.discard("p1", reason="conversation_deleted"))
    assert transport.events("p1", "run-1")[-1]["payload"] == {"code": "conversation_deleted"}
    assert docker.removed_containers == ["ct-p1"] and docker.volumes._items == {}


def test_restart_after_idle_does_not_expire_new_run(setup):
    manager, transport, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    transport.hello("p1")  # runner был подключён, потом контейнер удалён по простою
    asyncio.run(manager.retire("p1"))
    asyncio.run(manager.ensure_running("p1"))
    transport.message("p1", "run-1", "c1", "go")
    assert transport.expire(now=time.time() + 3600) == []
    assert docker.started == ["ct-p1", "ct-p1"]


def test_active_run_is_kept_but_fails_when_runner_never_connects(setup):
    manager, transport, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    transport.message("p1", "run-1", "c1", "go")
    now = time.time()
    asyncio.run(manager.supervise_once(now=now + 170))
    assert transport.status("p1", "run-1")["status"] == "queued"
    asyncio.run(manager.supervise_once(now=now + 1_000))
    assert docker.removed_containers == []
    assert transport.events("p1", "run-1")[-1]["payload"] == {"code": "runner_connect_timeout"}


def test_runner_exit_with_rejected_secret_fails_active_run(setup):
    manager, transport, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    transport.message("p1", "run-1", "c1", "go")
    docker.containers.get("ct-p1").exit(78)
    asyncio.run(manager.supervise_once())
    assert transport.events("p1", "run-1")[-1]["payload"] == {"code": "runner_config_rejected"}
    status = manager.describe("p1")
    assert status["state"] == "exited" and status["exit_code"] == 78


def test_docker_timestamps():
    assert _docker_time("0001-01-01T00:00:00Z") is None
    assert _docker_time("2026-10-25T10:00:00.123456789Z") == pytest.approx(1792922400.123456)


def test_bridge_tokens_are_dynamic_and_late_events_do_not_break_connection(tmp_path):
    transport = RunnerTransport(tmp_path / "transport.db")
    app = create_runner_app(transport, {})
    bridge = app.state.runner_bridge
    hello = {"v": 1, "type": "hello", "project_id": "p1", "runner_version": "0.1",
             "last_seq": 0, "session_id": None}
    headers = {"Authorization": "Bearer test-token-p1-0123456789"}
    transport.message("p1", "run-1", "c1", "go")
    transport.abandon("p1", "run-1", "runner_exited")
    with TestClient(app) as client:
        bridge.register("p1", "test-token-p1-0123456789")
        with pytest.raises(ValueError):
            bridge.register("p2", "test-token-p1-0123456789")
        with client.websocket_connect("/v1/runner", headers=headers) as ws:
            ws.send_json(hello)
            assert ws.receive_json()["type"] == "welcome"
            ws.send_json({"v": 1, "type": "event", "journal_seq": 7, "event": {
                "event_id": "00000000-0000-0000-0000-000000000007", "project_id": "p1",
                "run_id": "run-1", "occurred_at": "2026-10-25T10:00:00Z",
                "type": "message.created", "payload": {"role_id": None, "text": "late"}}})
            ws.send_json({"v": 1, "type": "heartbeat", "phase": "idle", "health": "ok",
                          "last_progress_at": 1.0})
            deadline = time.time() + 5
            while transport.project_state("p1")["phase"] != "idle":
                assert time.time() < deadline
                time.sleep(0.01)
            # Close the protocol session before TestClient tears down its portal.
            ws.close()
            while bridge.is_connected("p1"):
                assert time.time() < deadline
                time.sleep(0.01)
        assert transport.hello("p1") == 7
        bridge.revoke("p1")
        with pytest.raises(Exception):
            with client.websocket_connect("/v1/runner", headers=headers) as ws:
                ws.send_json(hello)
                ws.receive_json()


def test_materials_wait_until_runner_creates_project_dir(setup):
    from docker.errors import NotFound

    manager, _, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    container = docker.containers.get("ct-p1")
    attempts = []
    original = container.put_archive

    def put_archive(path, data):
        # Первые попытки — до seed_project: каталога /workspace/project ещё нет.
        attempts.append(path)
        if len(attempts) < 3:
            raise NotFound("Could not find the file /workspace/project")
        return original(path, data)

    container.put_archive = put_archive
    asyncio.run(manager.write_files("p1", "materials", {"a.md": b"x"}))

    assert len(attempts) == 3
    [(path, archive)] = container.archives
    assert path == "/workspace/project" and unpack(archive)["materials/a.md"][0] == b"x"


def test_materials_give_up_when_project_dir_never_appears(setup):
    from docker.errors import NotFound

    from backend.containers import ContainerError

    manager, _, docker = setup
    asyncio.run(manager.ensure_running("p1"))
    container = docker.containers.get("ct-p1")

    def put_archive(path, data):
        raise NotFound("missing")

    container.put_archive = put_archive
    with pytest.raises(ContainerError, match="runner_project_missing"):
        asyncio.run(manager.write_files("p1", "materials", {"a.md": b"x"}, wait_seconds=0.6))
