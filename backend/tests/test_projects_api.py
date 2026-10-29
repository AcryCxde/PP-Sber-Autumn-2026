"""Проекты CoreTeams: подготовка, этапы с версиями, материалы и изоляция владельца."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import backend.api.projects as projects_api
import backend.main as main_module
from backend.tests.auth_helpers import PASSWORD, sign_in
from backend.tests.test_history_api import FakeProcess, claude_lines, make_app


class FakeClaude:
    """Отвечает документом этапа и запоминает промпты и содержимое materials/."""

    def __init__(self):
        self.prompts: list[str] = []
        self.materials: list[dict[str, bytes]] = []

    async def __call__(self, prompt, workspace, resume_session_id=None, *, permission_mode=None):
        del resume_session_id, permission_mode
        self.prompts.append(prompt)
        folder = Path(workspace) / "materials"
        self.materials.append(
            {item.name: item.read_bytes() for item in folder.iterdir()} if folder.is_dir() else {}
        )
        return FakeProcess(claude_lines(f"# Результат {len(self.prompts)}\nТекст версии"))


@pytest.fixture
def claude(monkeypatch) -> FakeClaude:
    fake = FakeClaude()
    monkeypatch.setattr(main_module, "start_claude", fake)
    return fake


def project_command(project_id: str, kind: str = "stage", text: str | None = None, **extra) -> dict:
    return {
        "type": "project.run",
        "project_id": project_id,
        "client_request_id": extra.pop("client_request_id", str(uuid4())),
        "kind": kind,
        "text": text,
    }


def run_project(client: TestClient, command: dict) -> list[dict]:
    events = []
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json(command)
        while True:
            event = websocket.receive_json()
            events.append(event)
            if event["type"] in {"run.completed", "run.failed", "run.outcome_unknown", "command.error"}:
                return events


def ready_project(client: TestClient, *, example: bool = True) -> dict:
    project = client.post("/api/projects", json={"example": example}).json()
    return client.patch(
        f"/api/projects/{project['id']}",
        json={"answers": [["Больше практики"], ["Аудитория смешанная"], []], "custom": ["", "", "Тон автора"]},
    ).json()


def test_example_project_is_prefilled_and_listed(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        created = client.post("/api/projects", json={"example": True})
        listed = client.get("/api/projects").json()
        conversations = client.get("/api/conversations").json()
        standalone = client.get("/api/conversations?standalone=true").json()

    assert created.status_code == 201
    body = created.json()
    assert body["scenario"] == "course" and body["example"] is True
    assert body["name"] == "Исследования для начинающих дизайнеров"
    assert body["status"] == body["phase"] == "draft"
    assert [role["name"] for role in body["team"]] == ["Методист", "Исследователь аудитории", "Редактор"]
    assert [item["id"] for item in listed] == [body["id"]]
    # У проекта свой диалог владельца: он виден и в истории.
    assert [item["id"] for item in conversations] == [body["conversation_id"]]
    assert standalone == []


def test_full_journey_with_revision_and_approvals(tmp_path, claude) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        project = ready_project(client)
        project_id = project["id"]
        upload = client.post(
            f"/api/projects/{project_id}/files",
            files={"file": ("программа.md", "# Программа курса".encode(), "text/markdown")},
        )
        assert upload.status_code == 201

        first = run_project(client, project_command(project_id))
        assert first[-1]["type"] == "run.completed"
        state = client.get(f"/api/projects/{project_id}").json()
        assert state["status"] == "active" and state["phase"] == "review"
        assert state["results"][0][0]["content"].startswith("# Результат 1")

        # Задача уже передана команде: её не переписать, а дополнения — можно.
        assert client.patch(f"/api/projects/{project_id}", json={"problem": "другое"}).status_code == 409
        assert client.patch(f"/api/projects/{project_id}", json={"note": "Учесть выходные"}).status_code == 200

        revised = run_project(client, project_command(project_id, "revision", "Добавить риски"))
        assert revised[-1]["type"] == "run.completed"
        state = client.get(f"/api/projects/{project_id}").json()
        assert [item["version"] for item in state["results"][0]] == [1, 2]

        assert client.post(f"/api/projects/{project_id}/approve").json()["phase"] == "idle"
        run_project(client, project_command(project_id))
        client.post(f"/api/projects/{project_id}/approve")
        run_project(client, project_command(project_id))
        final = client.post(f"/api/projects/{project_id}/approve").json()
        done = client.post(f"/api/projects/{project_id}/finish").json()

    # Первый промпт собран из задачи, ответов и материалов, которые реально дошли до Claude.
    assert "Ученики долго изучают теорию" in claude.prompts[0]
    assert "Больше практики" in claude.prompts[0] and "Тон автора" in claude.prompts[0]
    assert "программа.md" in claude.prompts[0]
    assert claude.materials[0] == {"программа.md": "# Программа курса".encode()}
    # Доработка видит текущую версию и замечание; уточнение попадает в следующие запуски.
    assert "# Результат 1" in claude.prompts[1] and "Добавить риски" in claude.prompts[1]
    assert "Учесть выходные" in claude.prompts[1]
    # Следующий этап опирается на одобренную (последнюю) версию предыдущего.
    assert "Этап 2 из 3" in claude.prompts[2] and "# Результат 2" in claude.prompts[2]
    assert final["status"] == "ready"
    assert [item[-1]["accepted"] for item in final["results"]] == [True, True, True]
    assert final["results"][0][0]["accepted"] is False
    assert done["status"] == "done"
    assert final["runs_count"] == 4


def test_invalid_transitions_are_rejected(tmp_path, claude) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        empty = client.post("/api/projects", json={"example": False}).json()
        incomplete = run_project(client, project_command(empty["id"]))
        project = ready_project(client)
        early_revision = run_project(client, project_command(project["id"], "revision", "Сразу"))
        answer = run_project(client, project_command(project["id"], "answer", "Да"))
        approve = client.post(f"/api/projects/{project['id']}/approve")
        finish = client.post(f"/api/projects/{project['id']}/finish")
        run_project(client, project_command(project["id"]))
        second_start = run_project(client, project_command(project["id"]))
        bad_scenario = client.patch(f"/api/projects/{empty['id']}", json={"scenario": "crypto"})

    assert incomplete[-1]["code"] == "project_incomplete"
    assert early_revision[-1]["code"] == "project_state_conflict"
    assert answer[-1]["code"] == "project_state_conflict"
    assert approve.status_code == finish.status_code == 409
    assert second_start[-1]["code"] == "project_state_conflict"
    assert bad_scenario.status_code == 422
    assert len(claude.prompts) == 1


def test_repeated_client_request_replays_without_new_run(tmp_path, claude) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        project = ready_project(client)
        command = project_command(project["id"])
        run_project(client, command)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(command)
            replay = websocket.receive_json()

    assert replay["type"] == "run.accepted" and replay["idempotent_replay"] is True
    assert len(claude.prompts) == 1


def test_upload_rules(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(projects_api, "MAX_FILE_BYTES", 10)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        project_id = client.post("/api/projects", json={}).json()["id"]
        url = f"/api/projects/{project_id}/files"
        script = client.post(url, files={"file": ("run.sh", b"echo", "text/plain")})
        large = client.post(url, files={"file": ("big.txt", b"x" * 11, "text/plain")})
        traversal = client.post(url, files={"file": ("../../etc/notes.txt", b"ok", "text/plain")})
        replaced = client.post(url, files={"file": ("notes.txt", b"new", "text/plain")})
        files = replaced.json()["files"]
        removed = client.delete(f"{url}/{files[0]['id']}")

    assert script.status_code == 415
    assert large.status_code == 413
    assert traversal.status_code == 201 and traversal.json()["files"][0]["name"] == "notes.txt"
    # То же имя заменяет файл, а не дублирует его.
    assert [(item["name"], item["size_bytes"]) for item in files] == [("notes.txt", 3)]
    assert removed.json()["files"] == []


def test_foreign_project_is_hidden(tmp_path, claude) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client, "beta@example.com")
        client.cookies.clear()
        sign_in(client)
        project = ready_project(client)
        project_id = project["id"]
        client.post(f"/api/projects/{project_id}/files", files={"file": ("a.md", b"secret", "text/markdown")})
        run_project(client, project_command(project_id))

        client.cookies.clear()
        client.post("/api/auth/login", json={"email": "beta@example.com", "password": PASSWORD})
        base = f"/api/projects/{project_id}"
        responses = [
            client.get(base),
            client.patch(base, json={"note": "чужое"}),
            client.post(f"{base}/files", files={"file": ("b.md", b"x", "text/markdown")}),
            client.delete(f"{base}/files/{project['files'][0]['id'] if project['files'] else 'x'}"),
            client.post(f"{base}/approve"),
            client.post(f"{base}/finish"),
            client.delete(base),
        ]
        foreign_run = run_project(client, project_command(project_id, "revision", "чужое"))
        listed = client.get("/api/projects").json()

    assert [item.status_code for item in responses] == [404] * len(responses)
    assert foreign_run[-1]["code"] == "project_not_found"
    assert listed == []
    assert len(claude.prompts) == 1


def test_deleted_project_disappears(tmp_path, claude) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        project_id = ready_project(client)["id"]
        assert client.delete(f"/api/projects/{project_id}").status_code == 204
        assert client.get(f"/api/projects/{project_id}").status_code == 404
        assert client.get("/api/projects").json() == []
        assert client.get("/api/conversations").json() == []


def test_stage_result_is_last_message_not_interim_replies(tmp_path, monkeypatch) -> None:
    lines = claude_lines("# Карта проблем и концепция\nИтог")
    interim = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Изучаю материалы…"}]}})
    lines.insert(1, interim)

    async def fake_start_claude(prompt, workspace, resume_session_id=None, *, permission_mode=None):
        del prompt, workspace, resume_session_id, permission_mode
        return FakeProcess(list(lines))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        project = ready_project(client)
        run_project(client, project_command(project["id"]))
        state = client.get(f"/api/projects/{project['id']}").json()

    assert [item["content"] for item in state["results"][0]] == ["# Карта проблем и концепция\nИтог"]
