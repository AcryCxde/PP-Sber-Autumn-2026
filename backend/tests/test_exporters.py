import hashlib
import io
import zipfile

import pytest
from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader
from pptx import Presentation

from backend.exporters import ExportError, export_markdown
from backend.exporters.validation import (
    ExportValidationError,
    validate_docx,
    validate_export,
)


MARKDOWN = """# Итог

Русский текст и **важный вывод** с `кодом`.

## Проверки

- Первый пункт
- Второй пункт

| Поле | Значение |
|---|---|
| Формат | Проверен |
| Опасное значение | =2+2 |

```text
safe output
```
"""


def exported(output_format: str):
    return export_markdown(
        MARKDOWN,
        title="Проверенный результат",
        stem="result-v1",
        requested_format=output_format,
        max_output_bytes=2_000_000,
    )


def test_docx_export_renders_russian_headings_lists_tables_and_code() -> None:
    result = exported("docx")
    validate_docx(result.data)
    assert result.filename == "result-v1.docx"
    assert result.sha256 == hashlib.sha256(result.data).hexdigest()
    with zipfile.ZipFile(io.BytesIO(result.data)) as package:
        assert "word/document.xml" in package.namelist()
    document = Document(io.BytesIO(result.data))
    assert any("Русский текст" in paragraph.text for paragraph in document.paragraphs)
    assert document.tables[0].cell(1, 1).text == "Проверен"


def test_pdf_export_is_parseable_and_contains_pages() -> None:
    result = exported("pdf")
    validate_export(result.data, "pdf")
    assert result.data.startswith(b"%PDF-")
    assert len(PdfReader(io.BytesIO(result.data)).pages) >= 1
    assert result.content_type == "application/pdf"


def test_xlsx_export_is_parseable_and_neutralizes_formulas() -> None:
    result = exported("xlsx")
    validate_export(result.data, "xlsx")
    workbook = load_workbook(io.BytesIO(result.data), read_only=True, data_only=False)
    sheet = workbook[workbook.sheetnames[0]]
    assert sheet["B2"].value == "Проверен"
    assert sheet["B3"].value == "'=2+2"
    workbook.close()


def test_pptx_export_is_parseable_and_contains_content_slides() -> None:
    result = exported("pptx")
    validate_export(result.data, "pptx")
    presentation = Presentation(io.BytesIO(result.data))
    assert len(presentation.slides) >= 2
    assert presentation.slides[0].shapes.title.text == "Проверенный результат"


@pytest.mark.parametrize("output_format", ["docx", "pdf", "xlsx", "pptx"])
def test_every_export_has_matching_name_digest_and_detected_format(output_format: str) -> None:
    result = exported(output_format)
    assert result.filename == f"result-v1.{output_format}"
    assert result.requested_format == result.detected_format == output_format
    assert result.sha256 == hashlib.sha256(result.data).hexdigest()
    assert result.size_bytes == len(result.data)


def test_exports_reject_unknown_format_size_and_corruption() -> None:
    with pytest.raises(ExportError, match="invalid_output_format"):
        export_markdown("# A", title="A", stem="a", requested_format="exe", max_output_bytes=1_000_000)
    with pytest.raises(ExportError, match="artifact_export_too_large"):
        export_markdown("# A", title="A", stem="a", requested_format="docx", max_output_bytes=10)
    with pytest.raises(ExportValidationError):
        validate_docx(b"not-a-docx")
    for output_format in ("pdf", "xlsx", "pptx"):
        with pytest.raises(ExportValidationError):
            validate_export(b"not-an-export", output_format)
