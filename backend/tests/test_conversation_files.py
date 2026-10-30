"""Документы свободного диалога: загрузка до первого запроса, доступ агента и скачивание итога."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
import backend.materials as materials
from backend.tests.auth_helpers import sign_in
from backend.tests.test_history_api import (
    FakeProcess,
    claude_lines,
    make_app,
    run_command,
    write_tool_events,
)


class WritingClaude:
    """Читает materials/ и записывает итоговый документ через Write."""

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
        lines = claude_lines("Документ готов")
        lines[1:1] = write_tool_events("итог.md", "# Итог\nПо материалам")
        return FakeProcess(lines)


@pytest.fixture
def claude(monkeypatch) -> WritingClaude:
    fake = WritingClaude()
    monkeypatch.setattr(main_module, "start_claude", fake)
    return fake


def run(client: TestClient, command: dict) -> list[dict]:
    events = []
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json(command)
        while True:
            event = websocket.receive_json()
            events.append(event)
            if event["type"] in {"run.completed", "run.failed", "run.outcome_unknown", "command.error"}:
                return events


def test_attached_document_reaches_claude_and_result_downloads(tmp_path, claude) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        created = client.post("/api/conversations")
        conversation_id = created.json()["id"]
        url = f"/api/conversations/{conversation_id}/files"
        uploaded = client.post(url, files={"file": ("бриф.docx", b"PK\x03\x04docx", "application/octet-stream")})
        original = client.get(f"{url}/{uploaded.json()[0]['id']}")

        events = run(client, run_command("Сделай резюме брифа", conversation_id=conversation_id))
        artifact = next(event for event in events if event["type"] == "artifact.saved")
        result = client.get(f"/api/runs/{artifact['run_id']}/events/{artifact['seq']}/file")
        listed = client.get("/api/conversations?standalone=true").json()
        messages = client.get(f"/api/conversations/{conversation_id}/messages").json()

    assert created.status_code == 201 and created.json()["title"] == "Новый диалог"
    assert uploaded.status_code == 201
    assert [(item["name"], item["size_bytes"]) for item in uploaded.json()] == [("бриф.docx", 8)]
    assert original.content == b"PK\x03\x04docx"
    assert "attachment" in original.headers["content-disposition"]

    assert events[-1]["type"] == "run.completed"
    assert claude.materials == [{"бриф.docx": b"PK\x03\x04docx"}]
    assert "materials/" in claude.prompts[0] and "бриф.docx" in claude.prompts[0]
    # Приписка о файлах уходит только агенту: в истории остаётся запрос пользователя.
    assert messages[0]["content"] == "Сделай резюме брифа"
    assert [item["title"] for item in listed] == ["Сделай резюме брифа"]

    assert result.status_code == 200
    assert result.content == "# Итог\nПо материалам".encode()
    assert "filename*=UTF-8''%D0%B8%D1%82%D0%BE%D0%B3.md" in result.headers["content-disposition"]


def test_removed_document_disappears_from_workspace(tmp_path, claude) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        conversation_id = client.post("/api/conversations").json()["id"]
        url = f"/api/conversations/{conversation_id}/files"
        client.post(url, files={"file": ("a.md", b"a", "text/markdown")})
        files = client.post(url, files={"file": ("b.md", b"b", "text/markdown")}).json()
        run(client, run_command("Первый", conversation_id=conversation_id))
        remaining = client.delete(f"{url}/{files[0]['id']}").json()
        run(client, run_command("Второй", conversation_id=conversation_id))

    assert [item["name"] for item in remaining] == ["b.md"]
    assert claude.materials == [{"a.md": b"a", "b.md": b"b"}, {"b.md": b"b"}]


def test_chat_without_documents_gets_plain_prompt(tmp_path, claude) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        run(client, run_command("Привет"))

    assert claude.prompts == ["Привет"]
    assert claude.materials == [{}]


def test_upload_rules(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(materials, "MAX_FILE_BYTES", 10)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        url = f"/api/conversations/{client.post('/api/conversations').json()['id']}/files"
        script = client.post(url, files={"file": ("run.sh", b"echo", "text/plain")})
        large = client.post(url, files={"file": ("big.txt", b"x" * 11, "text/plain")})
        empty = client.post(url, files={"file": ("empty.txt", b"", "text/plain")})
        client.post(url, files={"file": ("../../notes.txt", b"old", "text/plain")})
        replaced = client.post(url, files={"file": ("notes.txt", b"newer", "text/plain")})

    assert script.status_code == 415
    assert large.status_code == 413
    assert empty.status_code == 422
    assert [(item["name"], item["size_bytes"]) for item in replaced.json()] == [("notes.txt", 5)]


def test_foreign_and_project_conversations_have_no_chat_files(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client, "beta@example.com")
        foreign_id = client.post("/api/conversations").json()["id"]
        foreign_file = client.post(
            f"/api/conversations/{foreign_id}/files",
            files={"file": ("secret.md", b"secret", "text/markdown")},
        ).json()[0]["id"]

        client.cookies.clear()
        sign_in(client)
        project_conversation = client.post("/api/projects", json={}).json()["conversation_id"]
        responses = [
            client.get(f"/api/conversations/{foreign_id}/files"),
            client.get(f"/api/conversations/{foreign_id}/files/{foreign_file}"),
            client.delete(f"/api/conversations/{foreign_id}/files/{foreign_file}"),
            client.post(
                f"/api/conversations/{foreign_id}/files",
                files={"file": ("x.md", b"x", "text/markdown")},
            ),
            client.post(
                f"/api/conversations/{project_conversation}/files",
                files={"file": ("x.md", b"x", "text/markdown")},
            ),
        ]

    assert [response.status_code for response in responses] == [404] * 5
