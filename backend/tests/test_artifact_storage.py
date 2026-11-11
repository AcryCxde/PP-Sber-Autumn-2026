"""Файлы агента в объектном хранилище: исходные байты, бинарные файлы и скачивание."""

import hashlib
import io
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.storage import MemoryStorage, StorageError, text_preview
from backend.tests.auth_helpers import sign_in
from backend.tests.fake_docker import FakeDocker
from backend.tests.test_history_api import FakeProcess, claude_lines, make_app, run_command, write_tool_events
from backend.tests.test_product_api import plan, post, wait_run
from backend.tests.test_runner_executor import FakeRunner, make_runner_app, receive_until_terminal


DOCX = b"PK\x03\x04" + bytes(range(256)) * 4  # не UTF-8: так выглядят docx, xlsx, pdf


def run_chat(client: TestClient, text: str) -> list[dict]:
    events = []
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json(run_command(text))
        while True:
            event = websocket.receive_json()
            events.append(event)
            if event["type"] in {"run.completed", "run.failed", "run.outcome_unknown"}:
                return events


@pytest.fixture
def storage() -> MemoryStorage:
    return MemoryStorage()


def test_cli_run_stores_written_and_script_generated_files(tmp_path, monkeypatch, storage) -> None:
    async def fake_claude(prompt, workspace, resume_session_id=None, *, permission_mode=None):
        del prompt, resume_session_id, permission_mode
        folder = Path(workspace)
        # Как скрипт агента: файл появляется в рабочей папке без инструмента Write.
        (folder / "out").mkdir(exist_ok=True)
        (folder / "out" / "report.docx").write_bytes(DOCX)
        (folder / ".cache").mkdir(exist_ok=True)
        (folder / ".cache" / "tmp.bin").write_bytes(b"skip")
        lines = claude_lines("Готово")
        lines[1:1] = write_tool_events("notes.md", "# Заметки\nтекст")
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_claude)
    app = make_app(tmp_path)
    app.state.storage = storage
    with TestClient(app) as client:
        sign_in(client)
        events = run_chat(client, "Сделай отчёт")
        saved = {e["path"]: e for e in events if e["type"] == "artifact.saved"}
        run_id = events[0]["run_id"]
        notes = client.get(f"/api/runs/{run_id}/events/{saved['notes.md']['seq']}/file")
        report = client.get(f"/api/runs/{run_id}/events/{saved['out/report.docx']['seq']}/file")

    assert sorted(saved) == ["notes.md", "out/report.docx"]
    assert saved["out/report.docx"]["truncated"] is False
    assert saved["out/report.docx"]["size_bytes"] == len(DOCX)
    assert notes.content == "# Заметки\nтекст".encode()
    assert report.content == DOCX
    assert report.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "report.docx" in report.headers["content-disposition"]
    assert len(storage.objects) == 2


def test_binary_file_without_storage_is_not_downloadable(tmp_path, monkeypatch) -> None:
    async def fake_claude(prompt, workspace, resume_session_id=None, *, permission_mode=None):
        del prompt, resume_session_id, permission_mode
        (Path(workspace) / "chart.png").write_bytes(DOCX)
        return FakeProcess(claude_lines("Готово"))

    monkeypatch.setattr(main_module, "start_claude", fake_claude)
    with TestClient(make_app(tmp_path)) as client:
        sign_in(client)
        events = run_chat(client, "Нарисуй график")
        [saved] = [e for e in events if e["type"] == "artifact.saved"]
        download = client.get(f"/api/runs/{events[0]['run_id']}/events/{saved['seq']}/file")

    # Без хранилища бинарный файл нельзя отдать из базы: запись есть, скачивания нет.
    assert saved["path"] == "chart.png" and saved["truncated"] is True
    assert download.status_code == 413


def test_storage_outage_keeps_run_and_text_download(tmp_path, monkeypatch) -> None:
    class BrokenStorage(MemoryStorage):
        async def put(self, key, data, content_type):
            raise StorageError("down")

    async def fake_claude(prompt, workspace, resume_session_id=None, *, permission_mode=None):
        del prompt, workspace, resume_session_id, permission_mode
        lines = claude_lines("Готово")
        lines[1:1] = write_tool_events("plan.md", "# План")
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_claude)
    app = make_app(tmp_path)
    app.state.storage = BrokenStorage()
    with TestClient(app) as client:
        sign_in(client)
        events = run_chat(client, "План")
        [saved] = [e for e in events if e["type"] == "artifact.saved"]
        download = client.get(f"/api/runs/{events[0]['run_id']}/events/{saved['seq']}/file")

    assert events[-1]["type"] == "run.completed"
    assert download.content == "# План".encode()


def test_runner_binary_artifact_is_stored_byte_for_byte(tmp_path, storage) -> None:
    docker = FakeDocker()

    def script(command, container):
        del command
        container.files["/workspace/project/report.docx"] = DOCX
        return [
            ("run.started", {}),
            ("artifact.created", {"artifact_id": "a1", "path": "report.docx", "commit_sha": "abc123",
                                  "role_id": None, "size_bytes": len(DOCX),
                                  "sha256": hashlib.sha256(DOCX).hexdigest()}),
            ("run.completed", {}),
        ]

    app = make_runner_app(tmp_path, docker)
    app.state.storage = storage
    with TestClient(app) as client:
        sign_in(client)
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json({"type": "run.create", "conversation_id": None,
                                     "client_request_id": str(uuid4()), "content": "Отчёт"})
                events = receive_until_terminal(websocket)
        finally:
            runner.halt.set()
        [saved] = [e for e in events if e["type"] == "artifact.saved"]
        download = client.get(f"/api/runs/{events[0]['run_id']}/events/{saved['seq']}/file")

    assert download.content == DOCX


def test_product_snapshot_links_files_for_download(tmp_path, monkeypatch, storage) -> None:
    async def fake_claude(prompt, workspace, resume_session_id=None, *, permission_mode=None):
        del prompt, resume_session_id, permission_mode
        (Path(workspace) / "budget.xlsx").write_bytes(DOCX)
        lines = claude_lines("Готово")
        lines[1:1] = write_tool_events("coreteams/understand.md", "# Разбор задачи\nФакты")
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_claude)
    app = make_app(tmp_path)
    app.state.storage = storage
    with TestClient(app) as client:
        sign_in(client)
        body = plan()
        project_id = post(client, "projects", body).json()["id"]
        run_id = post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand"}).json()["id"]
        run = wait_run(client, project_id, run_id)
        docs = {item["documentId"]: item for item in run["artifacts"]}
        sheet = client.get(docs["budget.xlsx"]["file"]["downloadUrl"])
        package = client.get(
            f"/api/v2/projects/{project_id}/runs/{run_id}/artifacts/{docs['budget.xlsx']['id']}/zip"
        )

    assert docs["understand"]["title"] == "Разбор задачи"
    assert docs["understand"]["file"]["name"] == "understand.md"
    assert docs["budget.xlsx"]["title"] == "budget.xlsx" and docs["budget.xlsx"]["content"] == ""
    assert docs["budget.xlsx"]["file"]["sizeBytes"] == len(DOCX)
    assert sheet.content == DOCX
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        assert archive.read("budget-v1.xlsx") == DOCX


def test_text_preview_limits() -> None:
    assert text_preview("абв".encode(), 100) == ("абв", False)
    assert text_preview(DOCX, 10_000) == (None, False)
    assert text_preview(b"x" * 11, 10) == (None, True)


def test_nul_bytes_are_not_lossless_database_text():
    assert text_preview(b"before\x00after", 100) == (None, False)


def test_cli_scan_persists_final_bytes_after_write_then_edit(tmp_path, monkeypatch, storage):
    async def fake_claude(prompt, workspace, resume_session_id=None, *, permission_mode=None):
        (Path(workspace) / "notes.md").write_text("final edited content")
        lines = claude_lines("Done")
        lines[1:1] = write_tool_events("notes.md", "initial Write content")
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_claude)
    app = make_app(tmp_path)
    app.state.storage = storage
    with TestClient(app) as client:
        sign_in(client)
        events = run_chat(client, "Write then edit")
        saved = [event for event in events if event["type"] == "artifact.saved"]
        assert len(saved) == 2
        download = client.get(f"/api/runs/{saved[-1]['run_id']}/events/{saved[-1]['seq']}/file")
        assert download.content == b"final edited content"


@pytest.mark.parametrize("file", [None, {"name": "report.docx", "sizeBytes": 9}])
def test_zip_refuses_missing_or_partial_original(file):
    from datetime import UTC, datetime
    from backend.models import Project
    from backend.product import DocumentVersion, ProductError, document_zip
    document = DocumentVersion("a1", "doc", "stage", 1, "Doc", "", datetime.now(UTC), "run", file)
    with pytest.raises(ProductError):
        document_zip(Project(product={}), document, None)


def test_zip_preserves_empty_text_original():
    from datetime import UTC, datetime
    from backend.models import Project
    from backend.product import DocumentVersion, document_zip
    document = DocumentVersion("a1", "doc", "stage", 1, "Doc", "", datetime.now(UTC), "run",
                               {"name": "empty.txt", "sizeBytes": 0})
    with zipfile.ZipFile(io.BytesIO(document_zip(Project(product={}), document, None))) as archive:
        assert archive.read("empty-v1.txt") == b""


async def test_s3_bounded_read_closes_stream_on_overflow():
    from types import SimpleNamespace
    from backend.storage import S3Storage
    body = io.BytesIO(b"too large")
    storage = object.__new__(S3Storage)
    storage.bucket = "test"
    storage.client = SimpleNamespace(get_object=lambda **kwargs: {"Body": body})
    with pytest.raises(StorageError):
        await storage.get_bounded("key", 3)
    assert body.closed


async def test_memory_storage_bounded_read_exact_limit():
    storage = MemoryStorage()
    await storage.put("key", b"123", "text/plain")
    assert await storage.get_bounded("key", 3) == b"123"
    with pytest.raises(StorageError):
        await storage.get_bounded("key", 2)


def test_product_snapshot_projects_pending_confirmation_error_code():
    from datetime import UTC, datetime
    from backend.models import ProductRun, ProjectRun, Run
    from backend.product import Chain
    run = Run(id="run", status="outcome_unknown", error_code="artifact_confirmation_pending", next_event_seq=2)
    link = ProjectRun(created_at=datetime.now(UTC), display_text="test")
    chain = Chain(ProductRun(id="product", stage_id="stage"), links=[(link, run)])
    assert chain.snapshot()["errorCode"] == "artifact_confirmation_pending"
    run.status = "completed"
    run.error_code = None
    assert "errorCode" not in chain.snapshot()
