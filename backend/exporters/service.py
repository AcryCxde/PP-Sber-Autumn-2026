from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from backend.exporters.docx import render_docx
from backend.exporters.pdf import render_pdf
from backend.exporters.pptx import render_pptx
from backend.exporters.validation import ExportValidationError, validate_export
from backend.exporters.xlsx import render_xlsx


CONTENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
RENDERERS: dict[str, Callable[..., bytes]] = {
    "docx": render_docx,
    "pdf": render_pdf,
    "xlsx": render_xlsx,
    "pptx": render_pptx,
}


class ExportError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExportResult:
    data: bytes
    filename: str
    content_type: str
    requested_format: str
    detected_format: str
    sha256: str
    size_bytes: int


def export_markdown(
    markdown: str,
    *,
    title: str,
    stem: str,
    requested_format: str,
    max_output_bytes: int,
) -> ExportResult:
    try:
        renderer = RENDERERS[requested_format]
    except KeyError as error:
        raise ExportError("invalid_output_format") from error
    try:
        data = renderer(markdown, title=title)
        if len(data) > max_output_bytes:
            raise ExportError("artifact_export_too_large")
        validate_export(data, requested_format)
    except ExportError:
        raise
    except ExportValidationError as error:
        raise ExportError("artifact_format_mismatch") from error
    except Exception as error:
        raise ExportError("artifact_export_failed") from error
    safe_stem = "".join(character for character in stem if character.isalnum() or character in "_-")[:80]
    filename = f"{safe_stem or 'result'}.{requested_format}"
    return ExportResult(
        data=data,
        filename=filename,
        content_type=CONTENT_TYPES[requested_format],
        requested_format=requested_format,
        detected_format=requested_format,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
