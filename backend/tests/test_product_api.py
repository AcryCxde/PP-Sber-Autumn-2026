"""Контракт V2 рабочего пространства: проект, файлы, Run, ответы, доработка, одобрение и ZIP."""

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.public_events import question_asked_event
from backend.repository import _append_event_in_transaction
from backend.tests.auth_helpers import sign_in
from backend.tests.test_history_api import FakeProcess, claude_lines, make_app, write_tool_events


class FakeClaude:
    """Сохраняет документы этапов в coreteams/ и запоминает промпты и materials/."""

    def __init__(self):
        self.prompts: list[str] = []
        self.materials: list[dict[str, bytes]] = []
        self.writes: list[list[tuple[str, str]]] = []

    async def __call__(self, prompt, workspace, resume_session_id=None, *, permission_mode=None):
        del resume_session_id, permission_mode
        self.prompts.append(prompt)
        folder = Path(workspace) / "materials"
        self.materials.append(
            {item.name: item.read_bytes() for item in folder.iterdir()} if folder.is_dir() else {}
        )
        writes = self.writes.pop(0) if self.writes else []
        lines = claude_lines(f"Готово {len(self.prompts)}")
        for path, content in reversed(writes):
            lines[1:1] = write_tool_events(path, content)
        return FakeProcess(lines)


@pytest.fixture
def claude(monkeypatch) -> FakeClaude:
    fake = FakeClaude()
    monkeypatch.setattr(main_module, "start_claude", fake)
    return fake


def plan(client_id: str | None = None, **changes) -> dict:
    body = {
        "clientProjectId": client_id or str(uuid4()),
        "title": "Кофейня у университета",
        "goal": "Решить, что проверить до аренды",
        "context": "Небольшой бюджет",
        "scenario": "other",
        "answers": ["Проверка спроса", "Студенты", "Без аренды"],
        "team": ["researcher", "analyst", "editor"],
        "teamNote": "Нужен взгляд маркетолога",
        "stages": [
            {"id": "understand", "title": "Разбор задачи", "goal": "Факты и вопросы"},
            {"id": "options", "title": "Варианты решения", "goal": "Варианты"},
        ],
    }
    return {**body, **changes}


def post(client: TestClient, path: str, body: dict, key: str | None = None):
    return client.post(f"/api/v2/{path}", json=body, headers={"Idempotency-Key": key or str(uuid4())})


def wait_run(client: TestClient, project_id: str, run_id: str, statuses=("completed", "failed", "waiting_for_input")) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = client.get(f"/api/v2/projects/{project_id}/runs/{run_id}").json()
        if run["status"] in statuses:
            return run
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not finish: {run}")


def started_project(client: TestClient, claude: FakeClaude) -> tuple[str, dict, dict]:
    body = plan()
    project_id = post(client, "projects", body).json()["id"]
    claude.writes.append([
        ("coreteams/understand.md", "# Разбор задачи\nФакты"),
        ("coreteams/options.md", "# Варианты решения\nВариант А"),
    ])
    created = post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand", "fileIds": []})
    assert created.status_code == 201, created.text
    return project_id, body, wait_run(client, project_id, created.json()["id"])


def test_create_project_is_idempotent_and_hidden_from_old_interface(tmp_path) -> None:
    with TestClient(make_app(tmp_path)) as client:
        sign_in(client)
        body = plan()
        first = post(client, "projects", body)
        again = post(client, "projects", body)
        unknown = post(client, "projects", plan(team=["researcher", "astrologer"]))
        listed = client.get("/api/projects").json()
        old_view = client.get(f"/api/projects/{first.json()['id']}")

    assert first.status_code == 201
    assert again.json() == first.json()
    assert unknown.status_code == 422 and unknown.json()["code"] == "unknown_role"
    assert listed == []
    assert old_view.status_code == 404


def test_upload_checks_hash_and_is_idempotent(tmp_path, claude) -> None:
    data = "Интервью: 12 человек".encode()
    digest = hashlib.sha256(data).hexdigest()
    with TestClient(make_app(tmp_path)) as client:
        sign_in(client)
        project_id = post(client, "projects", plan()).json()["id"]

        def upload(file_id: str, sha: str, name: str = "notes.txt"):
            return client.post(
                f"/api/v2/projects/{project_id}/files",
                files={"file": (name, data, "text/plain")},
                data={"clientFileId": file_id, "sha256": sha},
                headers={"Idempotency-Key": file_id},
            )

        mismatch = upload("f0", "0" * 64)
        first = upload("f1", digest)
        repeat = upload("f1", digest)
        same_name = upload("f2", digest)
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "sha256_mismatch"
    assert first.status_code == 201 and first.json()["sha256"] == digest
    assert repeat.json() == first.json()
    assert same_name.status_code == 201 and same_name.json()["id"] != first.json()["id"]


def test_initial_run_saves_stage_documents_and_replays_by_key(tmp_path, claude) -> None:
    data = b"materials"
    with TestClient(make_app(tmp_path)) as client:
        sign_in(client)
        body = plan()
        project_id = post(client, "projects", body).json()["id"]
        file_id = client.post(
            f"/api/v2/projects/{project_id}/files",
            files={"file": ("brief.md", data, "text/markdown")},
            data={"clientFileId": "local-1", "sha256": hashlib.sha256(data).hexdigest()},
        ).json()["id"]
        claude.writes.append([
            ("coreteams/understand.md", "# Разбор задачи\nФакты"),
            ("coreteams/options.md", "# Варианты решения\nВариант А"),
            ("notes/extra.md", "Черновик"),
        ])
        run_body = {**body, "stageId": "understand", "fileIds": [file_id]}
        key = str(uuid4())
        created = post(client, f"projects/{project_id}/runs", run_body, key)
        run = wait_run(client, project_id, created.json()["id"])
        replay = post(client, f"projects/{project_id}/runs", run_body, key)
        changed = post(client, f"projects/{project_id}/runs", {**run_body, "goal": "Другое"}, key)
        second = post(client, f"projects/{project_id}/runs", run_body)

    assert created.status_code == 201
    assert created.json()["status"] in {"queued", "running", "completed"}
    assert run["status"] == "completed"
    assert run["stageId"] == "understand"
    docs = {(item["stageId"], item["documentId"]): item for item in run["artifacts"]}
    assert docs[("understand", "understand")]["title"] == "Разбор задачи"
    assert docs[("options", "options")]["content"] == "# Варианты решения\nВариант А"
    assert docs[("understand", "notes/extra.md")]["version"] == 1
    assert all(item["saved"] and item["savedAt"].endswith("Z") for item in run["artifacts"])
    assert [message["role"] for message in run["messages"]] == ["user", "facilitator"]
    assert run["usage"] == {"requests": 1, "computeSeconds": 1.25, "cost": 0.0123, "currency": "USD"}
    assert "Исследователь" in claude.prompts[0] and "coreteams/options.md" in claude.prompts[0]
    assert "Нужен взгляд маркетолога" in claude.prompts[0]
    assert claude.materials[0] == {"brief.md": data}
    assert replay.status_code == 200 and replay.json() == run
    assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
    assert second.status_code == 409 and second.json()["code"] == "project_state_conflict"


def test_rework_creates_linked_run_with_next_version(tmp_path, claude) -> None:
    with TestClient(make_app(tmp_path)) as client:
        sign_in(client)
        project_id, body, first = started_project(client, claude)
        source = next(item for item in first["artifacts"] if item["documentId"] == "options")
        rework = {
            **body,
            "text": "Добавьте риски",
            "stageId": "options",
            "parentRunId": first["id"],
            "sourceArtifactId": source["id"],
            "documentId": "options",
            "sourceVersion": 1,
            "fileIds": [],
        }
        stale = post(client, f"projects/{project_id}/runs", {**rework, "sourceVersion": 2})
        claude.writes.append([("coreteams/options.md", "# Варианты решения\nС рисками")])
        created = post(client, f"projects/{project_id}/runs", rework)
        second = wait_run(client, project_id, created.json()["id"])
        old = client.get(f"/api/v2/projects/{project_id}/runs/{first['id']}").json()

    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"
    assert second["id"] != first["id"]
    assert second["parentRunId"] == first["id"] and second["sourceArtifactId"] == source["id"]
    assert [(item["documentId"], item["version"]) for item in second["artifacts"]] == [("options", 2)]
    assert second["messages"][0] == {**second["messages"][0], "role": "user", "text": "Добавьте риски"}
    assert "Вариант А" in claude.prompts[1] and "Добавьте риски" in claude.prompts[1]
    assert old == first


def test_answers_continue_the_same_run(tmp_path, claude) -> None:
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        body = plan()
        project_id = post(client, "projects", body).json()["id"]
        run_id = post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand"}).json()["id"]
        wait_run(client, project_id, run_id)

        async def ask() -> None:
            from sqlalchemy import select
            from backend.models import ProjectRun

            async with app.state.database.session() as session:
                link = await session.scalar(select(ProjectRun).where(ProjectRun.product_run_id == run_id))
                # Как runner: вопрос записывается до завершения запуска, здесь — после.
                await _append_event_in_transaction(session, link.run_id, allow_terminal=True, event_data=question_asked_event([
                    {"text": "Какой бюджет?", "header": "Бюджет", "options": [
                        {"label": "До 1 млн", "description": None},
                        {"label": "До 1 млн", "description": "дубль"},
                    ]},
                ]))
                await session.commit()

        client.portal.call(ask)
        waiting = client.get(f"/api/v2/projects/{project_id}/runs/{run_id}").json()
        question = waiting["question"]
        stale = post(client, f"projects/{project_id}/runs/{run_id}/answers",
                     {"questionId": "old", "answers": {"Какой бюджет?": "До 1 млн"}})
        mismatch = post(client, f"projects/{project_id}/runs/{run_id}/answers",
                        {"questionId": question["id"], "answers": {"Другой": "x"}})
        key = str(uuid4())
        answer = {"questionId": question["id"], "answers": {"Какой бюджет?": "До 1 млн"}}
        answered = post(client, f"projects/{project_id}/runs/{run_id}/answers", answer, key)
        done = wait_run(client, project_id, run_id, ("completed", "failed"))
        repeat = post(client, f"projects/{project_id}/runs/{run_id}/answers", answer, key)

    assert waiting["status"] == "waiting_for_input"
    assert question["questions"] == [{
        "text": "Какой бюджет?", "header": "Бюджет", "multi_select": False, "allow_custom": True,
        "options": [{"label": "До 1 млн", "description": None}],
    }]
    assert stale.status_code == 409 and stale.json()["code"] == "unknown_fork"
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "answers_mismatch"
    assert answered.status_code == 200 and answered.json()["id"] == run_id
    assert done["status"] == "completed" and done["revision"] > waiting["revision"]
    assert claude.prompts[1] == "До 1 млн"
    assert repeat.status_code == 200 and repeat.json()["id"] == run_id
    assert len(claude.prompts) == 2


def test_approval_receipt_and_zip_for_exact_version(tmp_path, claude) -> None:
    with TestClient(make_app(tmp_path)) as client:
        sign_in(client)
        project_id, _, run = started_project(client, claude)
        document = run["artifacts"][0]
        route = f"projects/{project_id}/runs/{run['id']}/artifacts/{document['id']}"
        wrong = post(client, f"{route}/approve", {"artifactId": document["id"], "version": 5})
        key = str(uuid4())
        approved = post(client, f"{route}/approve", {"artifactId": document["id"], "version": 1}, key)
        repeat = post(client, f"{route}/approve", {"artifactId": document["id"], "version": 1}, key)
        after = client.get(f"/api/v2/projects/{project_id}/runs/{run['id']}").json()
        archive = client.get(f"/api/v2/{route}/zip")

    assert wrong.status_code == 409 and wrong.json()["code"] == "version_conflict"
    receipt = approved.json()
    assert receipt["artifactId"] == document["id"] and receipt["version"] == 1 and receipt["approved"] is True
    assert repeat.json() == receipt
    assert after == run
    assert archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
        names = package.namelist()
        manifest = json.loads(package.read("manifest.json"))
        text = package.read(next(name for name in names if name.endswith("-v1.md"))).decode()
    assert text == document["content"]
    assert manifest["approval"] == receipt and manifest["runId"] == run["id"]


def test_other_user_cannot_see_product_project(tmp_path, claude) -> None:
    with TestClient(make_app(tmp_path)) as client:
        sign_in(client)
        project_id, _, run = started_project(client, claude)
        client.post("/api/auth/logout")
        sign_in(client, "beta@example.com")
        foreign = client.get(f"/api/v2/projects/{project_id}/runs/{run['id']}")
        client.cookies.clear()
        anonymous = client.get(f"/api/v2/projects/{project_id}/runs/{run['id']}")

    assert foreign.status_code == 404 and foreign.json()["code"] == "project_not_found"
    assert anonymous.status_code == 401


def test_rework_requires_completed_parent(tmp_path, claude) -> None:
    with TestClient(make_app(tmp_path)) as client:
        sign_in(client)
        body = plan()
        project_id = post(client, "projects", body).json()["id"]
        rework = post(client, f"projects/{project_id}/runs", {
            **body, "text": "x", "stageId": "understand", "parentRunId": str(uuid4()),
            "sourceArtifactId": "a1", "documentId": "understand", "sourceVersion": 1,
        })
    assert rework.status_code == 409 and rework.json()["code"] == "project_state_conflict"
