import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

WEB_ROOT = Path(__file__).resolve().parents[2] / "web-demo"


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: Any

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass(frozen=True)
class StorageApi:
    base_url: str
    data_dir: Path
    token: str

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        request_headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers["content-type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=payload,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=5) as response:
                return Response(response.status, response.read(), response.headers)
        except HTTPError as error:
            return Response(error.code, error.read(), error.headers)

    def save_artifact(
        self,
        *,
        project_id: str = "demo-main",
        run_id: str | None = None,
        operation_id: str | None = None,
        content: bytes = b"# Checkpoint\n",
    ) -> Response:
        return self.request(
            "POST",
            "/internal/artifacts",
            headers={"authorization": f"Bearer {self.token}"},
            body={
                "projectId": project_id,
                "runId": run_id or str(uuid4()),
                "operationId": operation_id or str(uuid4()),
                "title": "result.md",
                "content": content.decode("utf-8"),
                "contentSha256": hashlib.sha256(content).hexdigest(),
            },
        )


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def storage_api(tmp_path: Path) -> StorageApi:
    node = shutil.which("node")
    git = shutil.which("git")
    if node is None or git is None:
        pytest.skip("storage integration tests require node and git")

    port = unused_port()
    data_dir = tmp_path / "state"
    storage_root = tmp_path / "storage"
    token = secrets.token_urlsafe(32)
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment.update(
        {
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "DATA_DIR": str(data_dir),
            "STORAGE_ROOT_PATH": str(storage_root),
            "STORAGE_TOKEN": token,
        }
    )
    process = subprocess.Popen(
        [node, "server.js"],
        cwd=WEB_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    api = StorageApi(f"http://127.0.0.1:{port}", data_dir, token)

    try:
        for _ in range(50):
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"storage server exited early\nstdout:\n{stdout}\nstderr:\n{stderr}")
            try:
                if api.request("GET", "/health/ready").status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("storage server did not become ready")
        yield api
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_negative_isolation_matrix_rejects_foreign_ids(storage_api: StorageApi) -> None:
    run_id = str(uuid4())
    saved_response = storage_api.save_artifact(run_id=run_id)
    assert saved_response.status == 201
    artifact_id = saved_response.json()["artifactId"]
    foreign_user = {"x-demo-user": "demo-other"}

    project_read = storage_api.request(
        "GET",
        "/api/projects/demo-main/runs",
        headers=foreign_user,
    )
    project_write = storage_api.request(
        "POST",
        "/api/projects/demo-main/runs",
        headers={**foreign_user, "idempotency-key": str(uuid4())},
        body={"task": "foreign write"},
    )
    document_read = storage_api.request(
        "GET",
        f"/api/projects/demo-isolated/runs/{run_id}/events",
        headers=foreign_user,
    )
    version_read = storage_api.request(
        "GET",
        f"/api/projects/demo-isolated/artifacts/{artifact_id}",
        headers=foreign_user,
    )
    foreign_overwrite = storage_api.save_artifact(
        project_id="demo-isolated",
        run_id=run_id,
        content=b"# Foreign overwrite\n",
    )

    assert project_read.status == 404
    assert project_write.status == 404
    assert document_read.status == 404
    assert version_read.status == 404
    assert foreign_overwrite.status == 403


def test_idempotency_key_is_scoped_to_project_and_owner(storage_api: StorageApi) -> None:
    idempotency_key = str(uuid4())
    first = storage_api.request(
        "POST",
        "/api/projects/demo-main/runs",
        headers={"idempotency-key": idempotency_key},
        body={"task": "first owner"},
    )
    second = storage_api.request(
        "POST",
        "/api/projects/demo-isolated/runs",
        headers={
            "idempotency-key": idempotency_key,
            "x-demo-user": "demo-other",
        },
        body={"task": "second owner"},
    )

    assert first.status == 201
    assert second.status == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["projectId"] == "demo-main"
    assert second.json()["projectId"] == "demo-isolated"


def test_checkpoint_replay_is_idempotent_and_restores_original_bytes(
    storage_api: StorageApi,
) -> None:
    run_id = str(uuid4())
    operation_id = str(uuid4())
    content = "# Результат\n\nИсходные байты сохранены.\n".encode()
    expected_hash = hashlib.sha256(content).hexdigest()

    first_response = storage_api.save_artifact(
        run_id=run_id,
        operation_id=operation_id,
        content=content,
    )
    replay_response = storage_api.save_artifact(
        run_id=run_id,
        operation_id=operation_id,
        content=content,
    )

    assert first_response.status == 201
    assert replay_response.status == 200
    first = first_response.json()
    replay = replay_response.json()
    assert replay["artifactId"] == first["artifactId"]
    assert replay["commitSha"] == first["commitSha"]
    assert replay["contentSha256"] == expected_hash

    state = json.loads((storage_api.data_dir / "state.json").read_text(encoding="utf-8"))
    matching_versions = [
        artifact
        for artifact in state["artifacts"].values()
        if artifact["runId"] == run_id and artifact["contentSha256"] == expected_hash
    ]
    matching_operations = [
        operation
        for operation in state["operations"].values()
        if operation["runId"] == run_id and operation["contentSha256"] == expected_hash
    ]
    assert len(matching_versions) == 1
    assert len(matching_operations) == 1
    assert len(state["events"][run_id]) == 2

    restored = storage_api.request(
        "GET",
        f"/api/projects/demo-main/artifacts/{first['artifactId']}",
    )
    assert restored.status == 200
    assert restored.body == content
    assert hashlib.sha256(restored.body).hexdigest() == expected_hash


def test_new_writes_to_terminal_and_outcome_unknown_runs_are_rejected(
    storage_api: StorageApi,
) -> None:
    completed_run_id = str(uuid4())
    assert storage_api.save_artifact(run_id=completed_run_id).status == 201
    completed_write = storage_api.save_artifact(
        run_id=completed_run_id,
        content=b"# Late completed write\n",
    )
    assert completed_write.status == 409

    create_unknown = storage_api.request(
        "POST",
        "/api/projects/demo-main/runs",
        headers={"idempotency-key": str(uuid4())},
        body={"task": "unknown outcome", "simulateUnknown": True},
    )
    assert create_unknown.status == 201
    unknown_run_id = create_unknown.json()["id"]

    for _ in range(40):
        runs_response = storage_api.request("GET", "/api/projects/demo-main/runs")
        runs = {run["id"]: run for run in runs_response.json()}
        if runs[unknown_run_id]["status"] == "outcome_unknown":
            break
        time.sleep(0.1)
    else:
        pytest.fail("run did not reach outcome_unknown")

    unknown_write = storage_api.save_artifact(
        run_id=unknown_run_id,
        content=b"# Late unknown write\n",
    )
    assert unknown_write.status == 409
