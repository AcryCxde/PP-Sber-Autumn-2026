from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from backend.exporters import ExportError, export_markdown
from backend.exporters.validation import ExportValidationError, validate_export
from backend.models import Artifact, ArtifactExport, ProductRun, Project, ProjectRun
from backend.storage import StorageError


@dataclass(frozen=True)
class ProductExportOutcome:
    required: bool
    ready: bool
    error_code: str | None = None


async def _context(session, run_id: str):
    row = (await session.execute(
        select(ProductRun, Project)
        .join(ProjectRun, ProjectRun.product_run_id == ProductRun.id)
        .join(Project, Project.id == ProductRun.project_id)
        .where(ProjectRun.run_id == run_id)
    )).first()
    return row


async def ensure_requested_export(app_state: Any, run_id: str) -> ProductExportOutcome:
    settings = app_state.settings
    async with app_state.database.session() as session:
        row = await _context(session, run_id)
        if row is None:
            return ProductExportOutcome(required=False, ready=True)
        product_run, project = row
        output_format = product_run.output_format or "md"
        if output_format == "md":
            return ProductExportOutcome(required=False, ready=True)
        if not settings.product_export_enabled:
            return ProductExportOutcome(required=True, ready=False, error_code="artifact_export_failed")
        if output_format not in {"docx", "pdf", "xlsx", "pptx"}:
            return ProductExportOutcome(required=True, ready=False, error_code="invalid_output_format")
        stages = (project.product or {}).get("stages") or []
        if not stages:
            return ProductExportOutcome(required=True, ready=False, error_code="artifact_export_failed")
        final_stage = stages[-1]
        if product_run.parent_run_id and product_run.stage_id != final_stage["id"]:
            return ProductExportOutcome(required=False, ready=True)
        expected_path = f"coreteams/{final_stage['id']}.md"
        run_ids = list(await session.scalars(
            select(ProjectRun.run_id).where(ProjectRun.product_run_id == product_run.id)
        ))
        source_versions = list(await session.scalars(
            select(Artifact)
            .where(Artifact.run_id.in_(run_ids), Artifact.path == expected_path)
            .order_by(Artifact.created_at, Artifact.id)
        ))
        source = source_versions[-1] if source_versions else None
        if source is None:
            return ProductExportOutcome(required=True, ready=False, error_code="artifact_export_failed")
        existing = await session.scalar(select(ArtifactExport).where(
            ArtifactExport.source_artifact_id == source.id,
            ArtifactExport.requested_format == output_format,
        ))
        if existing is not None:
            if existing.status == "ready":
                return ProductExportOutcome(required=True, ready=True)
            existing.status = "pending"
            existing.error_code = None
            await session.commit()
            export_id = existing.id
        else:
            export = ArtifactExport(
                id=str(uuid4()),
                source_artifact_id=source.id,
                run_id=run_id,
                requested_format=output_format,
                status="pending",
            )
            session.add(export)
            await session.commit()
            export_id = export.id
        source_content = source.content
        source_size = source.size_bytes
        source_digest = source.sha256
        source_storage_key = source.storage_key
        title = final_stage.get("title") or project.name or "CoreTeams result"
        stem = f"{final_stage['id']}-v{len(source_versions)}"

    storage = getattr(app_state, "storage", None)
    if storage is None:
        return await _fail(app_state, export_id, "artifact_export_storage_failed")
    try:
        if source_storage_key:
            source_bytes = await storage.get_bounded(
                source_storage_key, settings.product_export_max_source_bytes,
            )
        elif source_content is not None:
            source_bytes = source_content.encode("utf-8")
        else:
            return await _fail(app_state, export_id, "artifact_export_failed")
        if len(source_bytes) > settings.product_export_max_source_bytes:
            return await _fail(app_state, export_id, "artifact_export_too_large")
        if source_size is not None and len(source_bytes) != source_size:
            return await _fail(app_state, export_id, "artifact_export_failed")
        if source_digest and hashlib.sha256(source_bytes).hexdigest() != source_digest:
            return await _fail(app_state, export_id, "artifact_export_failed")
        markdown = source_bytes.decode("utf-8")
        result = await asyncio.wait_for(
            asyncio.to_thread(
                export_markdown,
                markdown,
                title=title,
                stem=stem,
                requested_format=output_format,
                max_output_bytes=settings.product_export_max_output_bytes,
            ),
            timeout=settings.product_export_timeout_seconds,
        )
        key = f"exports/{run_id}/{export_id}"
        await storage.put(key, result.data, result.content_type)
        readback = await storage.get_bounded(key, settings.product_export_max_output_bytes)
        validate_export(readback, output_format)
        if len(readback) != result.size_bytes or hashlib.sha256(readback).hexdigest() != result.sha256:
            raise ExportValidationError("Stored export differs from generated bytes")
    except UnicodeDecodeError:
        return await _cleanup_and_fail(app_state, storage, export_id, locals().get("key"), "artifact_export_failed")
    except asyncio.TimeoutError:
        return await _cleanup_and_fail(app_state, storage, export_id, locals().get("key"), "artifact_export_failed")
    except ExportError as error:
        return await _cleanup_and_fail(app_state, storage, export_id, locals().get("key"), error.code)
    except ExportValidationError:
        return await _cleanup_and_fail(app_state, storage, export_id, locals().get("key"), "artifact_format_mismatch")
    except StorageError:
        return await _cleanup_and_fail(app_state, storage, export_id, locals().get("key"), "artifact_export_storage_failed")
    except Exception:
        return await _cleanup_and_fail(app_state, storage, export_id, locals().get("key"), "artifact_export_failed")

    async with app_state.database.session() as session:
        row = await session.get(ArtifactExport, export_id)
        if row is None:
            try:
                await storage.delete(key)
            except StorageError:
                pass
            return ProductExportOutcome(required=True, ready=False, error_code="artifact_export_failed")
        row.detected_format = result.detected_format
        row.status = "ready"
        row.filename = result.filename
        row.content_type = result.content_type
        row.storage_key = key
        row.sha256 = result.sha256
        row.size_bytes = result.size_bytes
        row.error_code = None
        await session.commit()
    return ProductExportOutcome(required=True, ready=True)


async def _cleanup_and_fail(app_state: Any, storage: Any, export_id: str, key: object, code: str) -> ProductExportOutcome:
    if isinstance(key, str):
        try:
            await storage.delete(key)
        except StorageError:
            pass
    return await _fail(app_state, export_id, code)


async def _fail(app_state: Any, export_id: str, code: str) -> ProductExportOutcome:
    async with app_state.database.session() as session:
        row = await session.get(ArtifactExport, export_id)
        if row is not None:
            row.status = "failed"
            row.error_code = code
            await session.commit()
    return ProductExportOutcome(required=True, ready=False, error_code=code)
