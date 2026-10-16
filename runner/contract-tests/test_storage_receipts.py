"""Opt-in fail-closed receipt probes; no HTTP server, DB or model is contacted."""

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

import ctrunner.storage as storage
from ctrunner.storage import ArtifactStorageClient, ArtifactStorageError

CONTENT = "# Проверка 👩🏽‍💻\r\n".encode("utf-8")
RUN_ID = "00000000-0000-4000-8000-000000000010"


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return self.body


@pytest.fixture
def client(tmp_path: Path) -> ArtifactStorageClient:
    (tmp_path / "result.md").write_bytes(CONTENT)
    return ArtifactStorageClient("http://storage.invalid", "synthetic-test-token", "demo-main", tmp_path)


def receipt(request: Any) -> dict[str, Any]:
    body = json.loads(request.data)
    return {
        "type": "artifact_saved", "artifactId": "artifact-1", "operationId": body["operationId"],
        "commitSha": "a" * 40, "contentSha256": body["contentSha256"],
        "gitPath": f"projects/demo-main/runs/{RUN_ID}/result.md", "sizeBytes": len(CONTENT),
    }


async def test_utf8_payload_hash_size_and_retry_id_are_stable(client, monkeypatch):
    captured = []

    def fake(request, timeout):
        assert request.get_header("Authorization") == "Bearer synthetic-test-token"
        assert timeout == 30.0
        body = json.loads(request.data)
        assert body["content"].encode("utf-8") == CONTENT
        assert body["contentSha256"] == hashlib.sha256(CONTENT).hexdigest()
        captured.append(body)
        return Response(json.dumps(receipt(request)).encode())

    monkeypatch.setattr(storage, "urlopen", fake)
    first = await client.save(RUN_ID)
    second = await client.save(RUN_ID)
    assert first == second
    assert captured[0]["operationId"] == captured[1]["operationId"]
    assert first["size_bytes"] == len(CONTENT)


@pytest.mark.parametrize("key", ["artifactId", "commitSha", "gitPath", "sizeBytes"])
async def test_missing_receipt_fields_fail_closed(client, monkeypatch, key):
    def fake(request, timeout):
        body = receipt(request)
        del body[key]
        return Response(json.dumps(body).encode())

    monkeypatch.setattr(storage, "urlopen", fake)
    with pytest.raises(ArtifactStorageError):
        await client.save(RUN_ID)


@pytest.mark.parametrize(("key", "value"), [
    ("artifactId", None), ("artifactId", ""),
    ("commitSha", "not-a-commit"), ("gitPath", "projects/foreign/runs/other/result.md"),
    ("sizeBytes", -1), ("sizeBytes", True), ("sizeBytes", "17"),
    ("sizeBytes", None), ("sizeBytes", len(CONTENT) + 1),
    ("contentSha256", "0" * 64), ("operationId", "foreign-operation"), ("type", "pending"),
])
async def test_invalid_receipt_fields_fail_closed(client, monkeypatch, key, value):
    def fake(request, timeout):
        body = receipt(request)
        body[key] = value
        return Response(json.dumps(body).encode())

    monkeypatch.setattr(storage, "urlopen", fake)
    with pytest.raises(ArtifactStorageError):
        await client.save(RUN_ID)


@pytest.mark.parametrize("body", [b"not-json", b"\xff", b"[]", b"null"])
async def test_invalid_response_shape_fails_closed(client, monkeypatch, body):
    monkeypatch.setattr(storage, "urlopen", lambda request, timeout: Response(body))
    with pytest.raises(ArtifactStorageError):
        await client.save(RUN_ID)


@pytest.mark.parametrize("error", [
    HTTPError("http://storage.invalid", 401, "unauthorized", {}, None),
    HTTPError("http://storage.invalid", 409, "conflict", {}, None),
    HTTPError("http://storage.invalid", 503, "unavailable", {}, None),
    URLError("unreachable"), TimeoutError("timeout"),
])
async def test_transport_failure_is_never_a_confirmed_save(client, monkeypatch, error):
    def fake(request, timeout):
        raise error

    monkeypatch.setattr(storage, "urlopen", fake)
    with pytest.raises(ArtifactStorageError):
        await client.save(RUN_ID)
