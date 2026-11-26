from __future__ import annotations

import io
import zipfile

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader
from pptx import Presentation


class ExportValidationError(ValueError):
    pass


def _ooxml(data: bytes, required: set[str], label: str) -> None:
    if not data or not zipfile.is_zipfile(io.BytesIO(data)):
        raise ExportValidationError(f"{label} is not a ZIP package")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        if not required <= names or archive.testzip() is not None:
            raise ExportValidationError(f"{label} package is incomplete or corrupt")


def validate_docx(data: bytes) -> None:
    _ooxml(data, {"[Content_Types].xml", "word/document.xml"}, "DOCX")
    try:
        document = Document(io.BytesIO(data))
    except Exception as error:
        raise ExportValidationError("DOCX cannot be parsed") from error
    if not any(paragraph.text.strip() for paragraph in document.paragraphs) and not document.tables:
        raise ExportValidationError("DOCX has no visible content")


def validate_pdf(data: bytes) -> None:
    if not data.startswith(b"%PDF-"):
        raise ExportValidationError("PDF signature is missing")
    try:
        reader = PdfReader(io.BytesIO(data))
        if not reader.pages:
            raise ExportValidationError("PDF has no pages")
    except ExportValidationError:
        raise
    except Exception as error:
        raise ExportValidationError("PDF cannot be parsed") from error


def validate_xlsx(data: bytes) -> None:
    _ooxml(data, {"[Content_Types].xml", "xl/workbook.xml"}, "XLSX")
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        if not workbook.sheetnames:
            raise ExportValidationError("XLSX has no sheets")
        workbook.close()
    except ExportValidationError:
        raise
    except Exception as error:
        raise ExportValidationError("XLSX cannot be parsed") from error


def validate_pptx(data: bytes) -> None:
    _ooxml(data, {"[Content_Types].xml", "ppt/presentation.xml"}, "PPTX")
    try:
        presentation = Presentation(io.BytesIO(data))
        if not presentation.slides:
            raise ExportValidationError("PPTX has no slides")
    except ExportValidationError:
        raise
    except Exception as error:
        raise ExportValidationError("PPTX cannot be parsed") from error


VALIDATORS = {
    "docx": validate_docx,
    "pdf": validate_pdf,
    "xlsx": validate_xlsx,
    "pptx": validate_pptx,
}


def validate_export(data: bytes, requested_format: str) -> None:
    try:
        validator = VALIDATORS[requested_format]
    except KeyError as error:
        raise ExportValidationError("Unsupported output format") from error
    validator(data)
