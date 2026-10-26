"""Подтверждённая выгрузка итогового Markdown из эфемерного workspace."""

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, final
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ctrunner.protocol import JsonValue

MAX_ARTIFACT_BYTES: Final = 10 * 1024 * 1024


class ArtifactStorageError(RuntimeError):
    """Внешнее хранилище не подтвердило запись артефакта."""


@final
@dataclass(frozen=True, slots=True)
class ArtifactStorageClient:
    base_url: str
    token: str = field(repr=False)
    project_id: str
    project_dir: Path
    timeout_s: float = 30.0

    async def save(self, turn_id: str) -> dict[str, JsonValue] | None:
        artifact = self.project_dir / "result.md"
        if not artifact.is_file():
            return None
        return await asyncio.to_thread(self._save, artifact, turn_id)

    def _save(self, artifact: Path, turn_id: str) -> dict[str, JsonValue]:
        try:
            content_bytes = artifact.read_bytes()
        except OSError as error:
            raise ArtifactStorageError("result.md недоступен для чтения") from error
        if not content_bytes:
            raise ArtifactStorageError("result.md пуст")
        if len(content_bytes) > MAX_ARTIFACT_BYTES:
            raise ArtifactStorageError("result.md превышает лимит 10 MiB")
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ArtifactStorageError("result.md должен быть UTF-8 Markdown") from error

        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        operation_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"coreteam:{self.project_id}:{turn_id}:{content_sha256}",
            )
        )
        payload = json.dumps(
            {
                "projectId": self.project_id,
                "runId": turn_id,
                "operationId": operation_id,
                "title": "result.md",
                "content": content,
                "contentSha256": content_sha256,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(  # noqa: S310 — URL проверен в RunnerConfig
            f"{self.base_url.rstrip('/')}/internal/artifacts",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            # URL проходит схему/host-валидацию на границе конфигурации.
            with urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                body = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise ArtifactStorageError("внешнее хранилище не подтвердило запись") from error
        try:
            confirmed = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArtifactStorageError("хранилище вернуло некорректное подтверждение") from error
        if (
            not isinstance(confirmed, dict)
            or confirmed.get("type") != "artifact_saved"
            or confirmed.get("operationId") != operation_id
            or confirmed.get("contentSha256") != content_sha256
        ):
            raise ArtifactStorageError("подтверждение хранилища не соответствует запросу")

        required = ("artifactId", "commitSha", "gitPath", "sizeBytes")
        if any(key not in confirmed for key in required):
            raise ArtifactStorageError("подтверждение хранилища неполно")
        return {
            "artifact_id": str(confirmed["artifactId"]),
            "operation_id": operation_id,
            "commit_sha": str(confirmed["commitSha"]),
            "content_sha256": content_sha256,
            "git_path": str(confirmed["gitPath"]),
            "size_bytes": int(confirmed["sizeBytes"]),
        }
