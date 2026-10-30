"""Объектное хранилище файлов, созданных агентом (S3-совместимое: SeaweedFS в docker-стеке).

В базе у артефакта остаются путь, текстовое превью и ссылка на объект; исходные байты —
в хранилище, поэтому бинарные и большие файлы скачиваются без перекодировки и обрезки.
Без S3_ENDPOINT_URL хранилище выключено и файлы, как раньше, есть только в базе.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from backend.config import Settings


class StorageError(RuntimeError):
    pass


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...


@dataclass(frozen=True)
class StoredFile:
    key: str
    sha256: str
    content_type: str
    size_bytes: int


def content_type_of(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path, strict=False)
    return guessed or "application/octet-stream"


async def store_file(storage: ObjectStorage | None, run_id: str, path: str, data: bytes) -> StoredFile | None:
    """Кладёт байты файла в хранилище; None — хранилище выключено."""
    if storage is None:
        return None
    stored = StoredFile(
        key=f"artifacts/{run_id}/{uuid4()}",
        sha256=hashlib.sha256(data).hexdigest(),
        content_type=content_type_of(path),
        size_bytes=len(data),
    )
    await storage.put(stored.key, data, stored.content_type)
    return stored


def text_preview(data: bytes, max_bytes: int) -> tuple[str | None, bool]:
    """(текст, обрезан ли): превью только у текстовых файлов в пределах лимита."""
    if len(data) > max_bytes:
        return None, True
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return None, False


class S3Storage:
    def __init__(self, settings: Settings):
        import boto3
        from botocore.config import Config

        self.bucket = settings.s3_bucket
        self.client: Any = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            # Адресация по пути: http://s3:8333/<bucket>/<key>, без поддоменов bucket.
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )

    async def ensure_bucket(self) -> None:
        def ensure() -> None:
            from botocore.exceptions import ClientError

            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError:
                self.client.create_bucket(Bucket=self.bucket)

        await asyncio.to_thread(ensure)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            await asyncio.to_thread(
                self.client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
            )
        except Exception as error:
            raise StorageError(f"Could not store {key}") from error

    async def get(self, key: str) -> bytes:
        def read() -> bytes:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

        try:
            return await asyncio.to_thread(read)
        except Exception as error:
            raise StorageError(f"Could not read {key}") from error


class MemoryStorage:
    """Хранилище в памяти для тестов."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[key][0]
        except KeyError:
            raise StorageError(f"Could not read {key}") from None


def create_storage(settings: Settings) -> S3Storage | None:
    return S3Storage(settings) if settings.s3_endpoint_url else None
