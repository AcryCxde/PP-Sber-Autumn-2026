import json
from pathlib import Path
from typing import Any

import pytest

import ctrunner.storage as storage
from ctrunner.storage import ArtifactStorageClient, ArtifactStorageError


class Response:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = json.dumps(body).encode()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return self.body


async def test_missing_result_does_not_call_storage(tmp_path: Path) -> None:
    client = ArtifactStorageClient(
        "http://storage", "storage-token-123", "demo-main", tmp_path
    )
    assert "storage-token" not in repr(client)
    assert await client.save("00000000-0000-4000-8000-000000000010") is None


async def test_confirmed_save_returns_git_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "result.md").write_bytes(b"# Result\n")
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> Response:
        payload = json.loads(request.data)
        captured.update(payload)
        assert timeout == 30.0
        return Response(
            {
                "type": "artifact_saved",
                "artifactId": "artifact-1",
                "operationId": payload["operationId"],
                "commitSha": "a" * 40,
                "contentSha256": payload["contentSha256"],
                "gitPath": f"projects/demo-main/runs/{payload['runId']}/result.md",
                "sizeBytes": len("# Result\n".encode()),
            }
        )

    monkeypatch.setattr(storage, "urlopen", fake_urlopen)
    client = ArtifactStorageClient(
        "http://storage", "storage-token-123", "demo-main", tmp_path
    )
    saved = await client.save("00000000-0000-4000-8000-000000000010")

    assert saved is not None
    assert saved["commit_sha"] == "a" * 40
    assert saved["content_sha256"] == captured["contentSha256"]
    assert captured["content"] == "# Result\n"


async def test_mismatched_confirmation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "result.md").write_bytes(b"# Result\n")

    def fake_urlopen(request: Any, timeout: float) -> Response:
        del request, timeout
        return Response(
            {
                "type": "artifact_saved",
                "operationId": "wrong",
                "contentSha256": "0" * 64,
            }
        )

    monkeypatch.setattr(storage, "urlopen", fake_urlopen)
    client = ArtifactStorageClient(
        "http://storage", "storage-token-123", "demo-main", tmp_path
    )
    with pytest.raises(ArtifactStorageError, match="не соответствует"):
        await client.save("00000000-0000-4000-8000-000000000010")
