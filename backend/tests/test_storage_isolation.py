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
PROJECT_ALPHA = "demo-main"
PROJECT_BETA = "demo-isolated"
ALPHA_USER = "demo-user"
BETA_USER = "demo-other"


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
    storage_root: Path
    token: str

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def write_state(self, state: dict[str, Any]) -> None:
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

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
        project_id: str,
        *,
        run_id: str | None = None,
        operation_id: str | None = None,
        content: str = "# Isolation fixture\n",
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
                "content": content,
                "contentSha256": hashlib.sha256(content.encode()).hexdigest(),
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
        pytest.skip("Storage isolation tests require node and git")

    port = unused_port()
    data_dir = tmp_path / "state"
    storage_root = tmp_path / "artifact-repository"
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
    api = StorageApi(f"http://127.0.0.1:{port}", data_dir, storage_root, token)

    try:
        for _ in range(50):
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"Storage server exited early\nstdout:\n{stdout}\nstderr:\n{stderr}")
            try:
                if api.request("GET", "/health/ready").status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("Storage server did not become ready")
        yield api
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def user_headers(user: str) -> dict[str, str]:
    return {"x-demo-user": user}


def assert_safe_rejection(api: StorageApi, response: Response, *forbidden: str) -> None:
    assert response.status in {400, 401, 403, 404, 409, 422, 503}
    text = response.body.decode("utf-8", errors="replace")
    lowered = text.lower()
    blocked = {
        str(api.data_dir),
        str(api.storage_root),
        api.token,
        "node:internal",
        "at servers",
        "traceback",
        *forbidden,
    }
    for value in blocked:
        assert value.lower() not in lowered


def save_fixture(api: StorageApi, project_id: str, marker: str) -> dict[str, Any]:
    response = api.save_artifact(project_id, content=f"# {marker}\n")
    assert response.status == 201
    return response.json()


def test_project_alpha_subject_sees_only_project_alpha(storage_api: StorageApi) -> None:
    response = storage_api.request("GET", "/api/projects", headers=user_headers(ALPHA_USER))
    assert response.status == 200
    assert [project["id"] for project in response.json()] == [PROJECT_ALPHA]


def test_project_alpha_subject_cannot_list_project_beta_runs(storage_api: StorageApi) -> None:
    response = storage_api.request(
        "GET",
        f"/api/projects/{PROJECT_BETA}/runs",
        headers=user_headers(ALPHA_USER),
    )
    assert response.status == 404
    assert_safe_rejection(storage_api, response, PROJECT_BETA)


def test_project_alpha_subject_cannot_create_project_beta_run(storage_api: StorageApi) -> None:
    response = storage_api.request(
        "POST",
        f"/api/projects/{PROJECT_BETA}/runs",
        headers={**user_headers(ALPHA_USER), "idempotency-key": str(uuid4())},
        body={"task": "foreign project write"},
    )
    assert response.status == 404
    assert_safe_rejection(storage_api, response, PROJECT_BETA)


def test_foreign_artifact_id_is_hidden_under_attackers_project(storage_api: StorageApi) -> None:
    artifact = save_fixture(storage_api, PROJECT_BETA, "BETA-PRIVATE-CONTENT")
    response = storage_api.request(
        "GET",
        f"/api/projects/{PROJECT_ALPHA}/artifacts/{artifact['artifactId']}",
        headers=user_headers(ALPHA_USER),
    )
    assert response.status == 404
    assert_safe_rejection(storage_api, response, "BETA-PRIVATE-CONTENT")


def test_foreign_artifact_download_is_hidden_from_foreign_owner(storage_api: StorageApi) -> None:
    artifact = save_fixture(storage_api, PROJECT_BETA, "BETA-DOWNLOAD-SECRET")
    own = storage_api.request(
        "GET",
        f"/api/projects/{PROJECT_BETA}/artifacts/{artifact['artifactId']}",
        headers=user_headers(BETA_USER),
    )
    assert own.status == 200
    assert own.body == b"# BETA-DOWNLOAD-SECRET\n"

    foreign = storage_api.request(
        "GET",
        f"/api/projects/{PROJECT_BETA}/artifacts/{artifact['artifactId']}",
        headers=user_headers(ALPHA_USER),
    )
    assert foreign.status == 404
    assert_safe_rejection(storage_api, foreign, "BETA-DOWNLOAD-SECRET")


def test_foreign_run_artifact_download_is_hidden(storage_api: StorageApi) -> None:
    run_id = str(uuid4())
    save_fixture_response = storage_api.save_artifact(
        PROJECT_BETA,
        run_id=run_id,
        content="# BETA-RUN-SECRET\n",
    )
    assert save_fixture_response.status == 201

    response = storage_api.request(
        "GET",
        f"/api/projects/{PROJECT_BETA}/runs/{run_id}/artifact",
        headers=user_headers(ALPHA_USER),
    )
    assert response.status == 404
    assert_safe_rejection(storage_api, response, "BETA-RUN-SECRET")


@pytest.mark.xfail(
    strict=True,
    reason="PPS-132 Storage finding: artifact metadata endpoint is not implemented, so ownership isolation cannot be verified",
)
def test_foreign_artifact_metadata_is_rejected_after_owner_positive_control(storage_api: StorageApi) -> None:
    artifact = save_fixture(storage_api, PROJECT_BETA, "BETA-METADATA")
    route = f"/api/projects/{PROJECT_BETA}/artifacts/{artifact['artifactId']}/metadata"
    own = storage_api.request("GET", route, headers=user_headers(BETA_USER))
    assert own.status == 200
    foreign = storage_api.request("GET", route, headers=user_headers(ALPHA_USER))
    assert foreign.status in {403, 404}
    assert_safe_rejection(storage_api, foreign, "BETA-METADATA")


@pytest.mark.xfail(
    strict=True,
    reason="PPS-132 Storage finding: artifact versions endpoint is not implemented, so ownership isolation cannot be verified",
)
def test_foreign_artifact_versions_are_rejected_after_owner_positive_control(storage_api: StorageApi) -> None:
    artifact = save_fixture(storage_api, PROJECT_BETA, "BETA-VERSIONS")
    route = f"/api/projects/{PROJECT_BETA}/artifacts/{artifact['artifactId']}/versions"
    own = storage_api.request("GET", route, headers=user_headers(BETA_USER))
    assert own.status == 200
    foreign = storage_api.request("GET", route, headers=user_headers(ALPHA_USER))
    assert foreign.status in {403, 404}
    assert_safe_rejection(storage_api, foreign, "BETA-VERSIONS")


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        ("POST", "/bindings", {"runId": "foreign-run"}),
        ("PATCH", "", {"filename": "changed.md"}),
        ("DELETE", "", None),
    ],
    ids=["read", "bind", "modify", "delete"],
)
@pytest.mark.xfail(
    strict=True,
    reason="PPS-132 Storage finding: material routes are not implemented, so material ownership isolation cannot be verified",
)
def test_foreign_material_operations_are_rejected_after_owner_positive_control(
    storage_api: StorageApi,
    method: str,
    suffix: str,
    body: dict[str, object] | None,
) -> None:
    material_id = str(uuid4())
    state = storage_api.state()
    state["materials"] = {
        material_id: {
            "id": material_id,
            "projectId": PROJECT_BETA,
            "filename": "beta-private.md",
            "gitPath": f"projects/{PROJECT_BETA}/materials/beta-private.md",
        }
    }
    storage_api.write_state(state)
    route = f"/api/projects/{PROJECT_BETA}/materials/{material_id}{suffix}"
    own = storage_api.request(method, route, headers=user_headers(BETA_USER), body=body)
    assert own.status in {200, 201, 204}
    foreign = storage_api.request(method, route, headers=user_headers(ALPHA_USER), body=body)
    assert foreign.status in {403, 404}
    assert_safe_rejection(storage_api, foreign, "beta-private.md")


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.xfail(
    strict=True,
    reason="PPS-132 Storage finding: approval routes are not implemented, so cross-project approval bypass cannot be verified",
)
def test_foreign_artifact_approval_is_rejected_after_owner_positive_control(
    storage_api: StorageApi,
    decision: str,
) -> None:
    artifact = save_fixture(storage_api, PROJECT_BETA, "BETA-APPROVAL")
    route = f"/api/projects/{PROJECT_BETA}/artifacts/{artifact['artifactId']}/{decision}"
    own = storage_api.request("POST", route, headers=user_headers(BETA_USER), body={})
    assert own.status in {200, 204}
    foreign = storage_api.request("POST", route, headers=user_headers(ALPHA_USER), body={})
    assert foreign.status in {403, 404}
    assert_safe_rejection(storage_api, foreign, "BETA-APPROVAL")


@pytest.mark.parametrize(
    "project_id",
    ["../demo-isolated", "%2e%2e%2fdemo-isolated", "/tmp/project-beta", "C:\\project-beta"],
    ids=["dot-dot", "encoded-dot-dot", "absolute-posix", "absolute-windows"],
)
def test_artifact_save_rejects_traversal_project_id_without_writes(
    storage_api: StorageApi,
    project_id: str,
) -> None:
    before = storage_api.state()
    response = storage_api.save_artifact(project_id)
    assert response.status == 404
    assert storage_api.state() == before
    assert_safe_rejection(storage_api, response, project_id)


@pytest.mark.parametrize(
    "run_id",
    ["../escape", "%2e%2e%2fescape", "/tmp/escape", "C:\\escape", "alpha/beta"],
    ids=["dot-dot", "encoded-dot-dot", "absolute-posix", "absolute-windows", "separator"],
)
@pytest.mark.xfail(
    strict=True,
    reason="PPS-132 Storage finding: invalid runId is rejected only after an attacker-controlled run record is persisted",
)
def test_artifact_save_rejects_traversal_run_id_without_state_mutation(
    storage_api: StorageApi,
    run_id: str,
) -> None:
    before = storage_api.state()
    response = storage_api.save_artifact(PROJECT_ALPHA, run_id=run_id)
    assert_safe_rejection(storage_api, response, run_id)
    assert storage_api.state() == before


@pytest.mark.parametrize(
    "operation_id",
    ["../escape", "%2e%2e%2fescape", "/tmp/escape", "C:\\escape"],
    ids=["dot-dot", "encoded-dot-dot", "absolute-posix", "absolute-windows"],
)
@pytest.mark.xfail(
    strict=True,
    reason="PPS-132 Storage finding: invalid operationId remains in storage state after the request is rejected",
)
def test_artifact_save_rejects_traversal_operation_id_without_state_mutation(
    storage_api: StorageApi,
    operation_id: str,
) -> None:
    response = storage_api.save_artifact(PROJECT_ALPHA, operation_id=operation_id)
    assert_safe_rejection(storage_api, response, operation_id)
    assert operation_id not in storage_api.state()["operations"]


@pytest.mark.parametrize(
    "encoded_id",
    ["..%2Fprivate", "%2e%2e%2fprivate", "%2Ftmp%2Fprivate"],
    ids=["encoded-slash", "encoded-dot-dot", "encoded-absolute"],
)
def test_download_url_traversal_is_rejected_without_error_leaks(
    storage_api: StorageApi,
    encoded_id: str,
) -> None:
    response = storage_api.request(
        "GET",
        f"/api/projects/{PROJECT_ALPHA}/artifacts/{encoded_id}",
        headers=user_headers(ALPHA_USER),
    )
    assert_safe_rejection(storage_api, response, encoded_id)


@pytest.mark.xfail(
    strict=True,
    reason="PPS-132 Storage finding: a pre-existing project-directory symlink allows a write outside the artifact repository",
)
def test_storage_symlink_cannot_escape_project_prefix(storage_api: StorageApi, tmp_path: Path) -> None:
    outside = tmp_path / "outside-project"
    outside.mkdir()
    projects = storage_api.storage_root / "projects"
    projects.mkdir(exist_ok=True)
    project_link = projects / PROJECT_ALPHA
    try:
        project_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(project_link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip(f"Directory links are unavailable: {junction.stderr or junction.stdout}")

    run_id = str(uuid4())
    response = storage_api.save_artifact(PROJECT_ALPHA, run_id=run_id)
    assert_safe_rejection(storage_api, response, str(outside))
    assert not (outside / "runs" / run_id / "result.md").exists()


def test_foreign_rejection_does_not_leak_paths_stack_or_content(storage_api: StorageApi) -> None:
    artifact = save_fixture(storage_api, PROJECT_BETA, "BETA-RESPONSE-SECRET")
    response = storage_api.request(
        "GET",
        f"/api/projects/{PROJECT_ALPHA}/artifacts/{artifact['artifactId']}",
        headers=user_headers(ALPHA_USER),
    )
    assert_safe_rejection(storage_api, response, "BETA-RESPONSE-SECRET", artifact["gitPath"])


def test_rejected_path_does_not_leak_into_public_events(storage_api: StorageApi) -> None:
    attack_marker = "../BETA-PATH-MUST-NOT-BE-PUBLISHED"
    response = storage_api.save_artifact(PROJECT_ALPHA, run_id=attack_marker)
    assert_safe_rejection(storage_api, response, attack_marker)

    events = storage_api.request(
        "GET",
        f"/api/projects/{PROJECT_ALPHA}/runs/{attack_marker}/events",
        headers=user_headers(ALPHA_USER),
    )
    if events.status == 200:
        assert attack_marker.encode() not in events.body
        assert str(storage_api.storage_root).encode() not in events.body
        assert storage_api.token.encode() not in events.body
    else:
        assert_safe_rejection(storage_api, events, attack_marker)


def test_public_artifact_events_do_not_expose_git_path_or_content(storage_api: StorageApi) -> None:
    marker = "BETA-EVENT-CONTENT-SECRET"
    artifact = save_fixture(storage_api, PROJECT_BETA, marker)
    events = storage_api.request(
        "GET",
        f"/api/projects/{PROJECT_BETA}/runs/{artifact['runId']}/events",
        headers=user_headers(BETA_USER),
    )
    assert events.status == 200
    serialized = events.body.decode("utf-8")
    assert marker not in serialized
    assert artifact["gitPath"] not in serialized
    assert str(storage_api.storage_root) not in serialized
    assert storage_api.token not in serialized
