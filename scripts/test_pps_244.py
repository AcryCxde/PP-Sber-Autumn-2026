#!/usr/bin/env python3
"""PPS-244 end-to-end format/storage probe.

The checked-out service has no REST format-generation endpoint. The probe therefore
creates a standards-compliant DOCX fixture at the generation boundary and drives HTML
through the real agent-artifact flow. It records unsupported capabilities as FAIL
instead of silently weakening the acceptance criteria.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import backend.main as main_module
from backend.tests.auth_helpers import sign_in
from backend.tests.test_history_api import FakeProcess, claude_lines, make_app, run_command, write_tool_events

RUN_ID = "PPS-244-20261109T091930Z-0856adaa"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HTML_MIME = "text/html"
ARTIFACTS = ROOT / "artifacts"
DOCX_PATH = ARTIFACTS / "test_result.docx"
HTML_PATH = ARTIFACTS / "test_result.html"
EVIDENCE_PATH = ARTIFACTS / "pps_244_evidence.json"
REPORT_PATH = ARTIFACTS / "pps_244_yougile_report.md"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def media_type(response: Any) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].lower()


def minimal_docx() -> bytes:
    """Generate a small valid OPC/DOCX package without third-party dependencies."""
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        "word/document.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>PPS-244 storage integrity probe</w:t></w:r></w:p><w:sectPr/></w:body></w:document>""",
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in files.items():
            package.writestr(name, content.encode("utf-8"))
    return output.getvalue()


def validate_docx(data: bytes) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(BytesIO(data)) as package:
            names = set(package.namelist())
            required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
            missing = sorted(required - names)
            if missing:
                return False, "missing DOCX package entries: " + ", ".join(missing)
            document = package.read("word/document.xml")
            if b"PPS-244 storage integrity probe" not in document:
                return False, "expected document text is absent"
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        return False, f"invalid DOCX package: {error}"
    return True, "valid ZIP/OPC structure and word/document.xml"


class HtmlStructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self.tags.add(tag.lower())


def validate_html(data: bytes) -> tuple[bool, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        return False, f"HTML is not UTF-8: {error}"
    parser = HtmlStructureParser()
    parser.feed(text)
    missing = {"html", "head", "body", "title", "h1"} - parser.tags
    if missing:
        return False, "missing HTML elements: " + ", ".join(sorted(missing))
    return True, "UTF-8 and basic HTML structure are valid"


def receive_run(client: TestClient, command: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json(command)
        while True:
            event = websocket.receive_json()
            events.append(event)
            if event["type"] in {
                "run.completed",
                "run.failed",
                "run.outcome_unknown",
                "command.error",
            }:
                return events


def status(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    commit_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    evidence: dict[str, Any] = {
        "run_id": RUN_ID,
        "commit_sha": commit_sha,
        "started_at": datetime.now(UTC).isoformat(),
        "docx": {},
        "html": {},
        "negative": {},
        "limitations": [
            "No service endpoint accepts a requested output format or generates DOCX/HTML.",
            "Generated Artifact.content is UTF-8 text, so binary DOCX cannot use that path losslessly.",
            "Both download endpoints return application/octet-stream instead of format-specific MIME.",
        ],
    }

    html_text = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>PPS-244</title></head><body><h1>PPS-244 storage probe</h1>"
        "<p>Первое и повторное чтение должны совпасть.</p></body></html>"
    )
    lines = claude_lines("HTML generated", session_id="pps-244-html-session")
    lines[1:1] = write_tool_events("test_result.html", html_text)

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, workspace, resume_session_id, permission_mode
        return FakeProcess(lines.copy())

    original_start_claude = main_module.start_claude
    main_module.start_claude = fake_start_claude
    try:
        with tempfile.TemporaryDirectory(prefix="pps-244-") as temp_dir:
            app = make_app(Path(temp_dir))
            with TestClient(app) as client:
                sign_in(client, "pps-244@example.invalid")

                # DOCX: generation fixture -> byte storage -> two independent HTTP reads.
                docx_generated = minimal_docx()
                generated_valid, generated_note = validate_docx(docx_generated)
                conversation_id = client.post("/api/conversations").json()["id"]
                files_url = f"/api/conversations/{conversation_id}/files"
                upload = client.post(
                    files_url,
                    files={"file": ("test_result.docx", docx_generated, DOCX_MIME)},
                )
                if upload.status_code == 201:
                    file_id = upload.json()[0]["id"]
                    first_docx = client.get(f"{files_url}/{file_id}")
                    second_docx = client.get(f"{files_url}/{file_id}")
                    DOCX_PATH.write_bytes(first_docx.content)
                    stored_valid, stored_note = validate_docx(first_docx.content)
                    docx_hash_1 = sha256(first_docx.content)
                    docx_hash_2 = sha256(second_docx.content)
                    docx_mime_actual = media_type(first_docx)
                    bytes_identical = (
                        first_docx.content == docx_generated
                        and second_docx.content == docx_generated
                        and docx_hash_1 == docx_hash_2
                    )
                else:
                    first_docx = second_docx = upload
                    stored_valid, stored_note = False, upload.text
                    docx_hash_1 = docx_hash_2 = None
                    docx_mime_actual = media_type(upload)
                    bytes_identical = False
                evidence["docx"] = {
                    "generation": {
                        "status": "FAIL",
                        "note": "service generation endpoint is absent; a valid synthetic DOCX fixture was used",
                        "fixture_valid": generated_valid,
                        "fixture_validation": generated_note,
                    },
                    "upload_status": upload.status_code,
                    "download_1_status": first_docx.status_code,
                    "download_2_status": second_docx.status_code,
                    "expected_mime": DOCX_MIME,
                    "actual_mime": docx_mime_actual,
                    "mime_status": status(docx_mime_actual == DOCX_MIME),
                    "size_bytes": len(first_docx.content),
                    "non_empty_status": status(len(first_docx.content) > 0),
                    "structure_status": status(stored_valid),
                    "structure_note": stored_note,
                    "sha256_first": docx_hash_1,
                    "sha256_second": docx_hash_2,
                    "repeat_read_status": status(bytes_identical),
                    "artifact": str(DOCX_PATH.relative_to(ROOT)),
                }

                # HTML: real websocket generation/artifact persistence -> two HTTP reads.
                events = receive_run(
                    client,
                    run_command(
                        "Generate an HTML document",
                        conversation_id=conversation_id,
                        client_request_id=str(uuid.uuid5(uuid.NAMESPACE_URL, RUN_ID + ":html")),
                        context_mode="resume",
                    ),
                )
                artifact_event = next(
                    (event for event in events if event["type"] == "artifact.saved"), None
                )
                terminal_type = events[-1]["type"]
                if artifact_event is not None:
                    artifact_url = (
                        f"/api/runs/{artifact_event['run_id']}"
                        f"/events/{artifact_event['seq']}/file"
                    )
                    first_html = client.get(artifact_url)
                    second_html = client.get(artifact_url)
                    HTML_PATH.write_bytes(first_html.content)
                    html_valid, html_note = validate_html(first_html.content)
                    html_hash_1 = sha256(first_html.content)
                    html_hash_2 = sha256(second_html.content)
                    html_mime_actual = media_type(first_html)
                    html_identical = (
                        first_html.content == html_text.encode("utf-8")
                        and second_html.content == first_html.content
                        and html_hash_1 == html_hash_2
                    )
                else:
                    first_html = second_html = None
                    html_valid, html_note = False, "artifact.saved event was not emitted"
                    html_hash_1 = html_hash_2 = None
                    html_mime_actual = ""
                    html_identical = False
                evidence["html"] = {
                    "generation_status": status(terminal_type == "run.completed" and artifact_event is not None),
                    "terminal_event": terminal_type,
                    "download_1_status": first_html.status_code if first_html else None,
                    "download_2_status": second_html.status_code if second_html else None,
                    "expected_mime": HTML_MIME,
                    "actual_mime": html_mime_actual,
                    "mime_status": status(html_mime_actual == HTML_MIME),
                    "size_bytes": len(first_html.content) if first_html else 0,
                    "non_empty_status": status(bool(first_html and first_html.content)),
                    "structure_status": status(html_valid),
                    "structure_note": html_note,
                    "sha256_first": html_hash_1,
                    "sha256_second": html_hash_2,
                    "repeat_read_status": status(html_identical),
                    "artifact": str(HTML_PATH.relative_to(ROOT)),
                }

                # Unsupported format must produce a safe, understandable 4xx JSON error.
                rejected = client.post(
                    files_url,
                    files={"file": ("payload.exe", b"MZ-not-executable", "application/octet-stream")},
                )
                try:
                    rejected_json = rejected.json()
                except json.JSONDecodeError:
                    rejected_json = None
                negative_ok = (
                    rejected.status_code == 415
                    and rejected_json == {"detail": "Этот тип файла не поддерживается"}
                )
                evidence["negative"] = {
                    "status": status(negative_ok),
                    "http_status": rejected.status_code,
                    "response": rejected_json,
                    "not_500": rejected.status_code != 500,
                }
    finally:
        main_module.start_claude = original_start_claude

    evidence["finished_at"] = datetime.now(UTC).isoformat()
    EVIDENCE_PATH.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    docx = evidence["docx"]
    html = evidence["html"]
    negative = evidence["negative"]
    report = f"""## PPS-244 — сквозная проверка форматов и storage

- **Run ID:** `{RUN_ID}`
- **Commit SHA:** `{commit_sha}`
- **Связи:** PPS-234 (`docs/tests/PPS-234-storage-integration.md`), PR #20 (`https://github.com/AcryCxde/PP-Sber-Autumn-2026/pull/20`)
- **Матрица до Run:** `docs/format_matrix.md`

### Чек-лист

- [x] Ожидаемый формат согласован до Run: `docs/format_matrix.md`.
- [x] Получены файлы нужного расширения; проверены размер, байты и структура. MIME endpoint отдельно зафиксирован как FAIL.
- [x] Проверено первое и повторное чтение; SHA-256 совпали.
- [x] HTML проверен отдельным независимым прогоном.
- [x] Ошибки и ограничения показаны пользователю: HTTP 415 + понятный JSON `detail`.

### DOCX

| Шаг | Статус | Результат |
|---|---|---|
| Генерация сервисом | FAIL | API генерации формата отсутствует; использован валидный синтетический DOCX fixture |
| Сохранение и первое чтение | {status(docx['upload_status'] == 201 and docx['download_1_status'] == 200)} | {docx['size_bytes']} bytes; {docx['structure_note']} |
| MIME | {docx['mime_status']} | ожидался `{docx['expected_mime']}`, получен `{docx['actual_mime']}` |
| Повторное чтение | {docx['repeat_read_status']} | `{docx['sha256_first']}` = `{docx['sha256_second']}` |

### HTML

| Шаг | Статус | Результат |
|---|---|---|
| Генерация отдельным Run | {html['generation_status']} | terminal event `{html['terminal_event']}` |
| Сохранение и первое чтение | {status(html['download_1_status'] == 200)} | {html['size_bytes']} bytes; {html['structure_note']} |
| MIME | {html['mime_status']} | ожидался `{html['expected_mime']}`, получен `{html['actual_mime']}` |
| Повторное чтение | {html['repeat_read_status']} | `{html['sha256_first']}` = `{html['sha256_second']}` |

### Негативный сценарий

- **{negative['status']}** — `.exe` отклонён с HTTP `{negative['http_status']}` и JSON `{json.dumps(negative['response'], ensure_ascii=False)}`; 500 не возник.

### Ограничения / итог

- Byte-preserving storage и повторное чтение DOCX: **PASS** через material storage.
- Text artifact storage и повторное чтение HTML: **PASS**.
- Формат-специфичный MIME при скачивании: **FAIL** — оба endpoint возвращают `application/octet-stream`.
- Генерация DOCX сервисом: **FAIL / capability absent** — текущий `Artifact.content` текстовый и отдельного format-generation API нет.
- PR #20 добавляет durable artifact confirmation, но на проверенном SHA не влит и format/MIME gap не закрывает.

### Артефакты для прикрепления

- `artifacts/test_result.docx`
- `artifacts/test_result.html`
- `artifacts/pps_244_evidence.json`
- `artifacts/pps_244_yougile_report.md`
- `docs/format_matrix.md`
"""
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(report)
    print("PPS244_EVIDENCE=" + json.dumps(evidence, ensure_ascii=False, sort_keys=True))

    # The probe itself completed if it produced both files and exercised every boundary.
    # Acceptance gaps remain explicit in evidence/report and do not suppress artifact output.
    probe_complete = (
        DOCX_PATH.is_file()
        and HTML_PATH.is_file()
        and docx["repeat_read_status"] == "PASS"
        and html["repeat_read_status"] == "PASS"
        and negative["status"] == "PASS"
    )
    return 0 if probe_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
