from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from backend.exporters.markdown import Block, parse_markdown


DANGEROUS_PREFIXES = ("=", "+", "-", "@")


def _safe(value: str) -> str:
    return f"'{value}" if value.startswith(DANGEROUS_PREFIXES) else value


def _sheet_name(value: str, used: set[str]) -> str:
    base = "".join(character for character in value if character not in "[]:*?/\\")[:31] or "Таблица"
    candidate = base
    number = 2
    while candidate in used:
        suffix = f" {number}"
        candidate = f"{base[:31-len(suffix)]}{suffix}"
        number += 1
    used.add(candidate)
    return candidate


def _style_sheet(sheet) -> None:
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="142B35")
        cell.fill = PatternFill("solid", fgColor="E8EEF9")
    for row in sheet.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for column in range(1, sheet.max_column + 1):
        width = max((len(str(sheet.cell(row, column).value or "")) for row in range(1, sheet.max_row + 1)), default=8)
        sheet.column_dimensions[get_column_letter(column)].width = min(max(width + 2, 12), 60)


def render_xlsx(markdown: str, *, title: str) -> bytes:
    blocks = parse_markdown(markdown)
    workbook = Workbook()
    workbook.remove(workbook.active)
    used: set[str] = set()
    tables = [block for block in blocks if block.kind == "table"]
    if tables:
        for index, block in enumerate(tables, start=1):
            sheet = workbook.create_sheet(_sheet_name(f"Таблица {index}", used))
            for row in block.rows:
                sheet.append([_safe(value) for value in row])
            _style_sheet(sheet)
    else:
        sheet = workbook.create_sheet(_sheet_name("Результат", used))
        sheet.append(["Раздел", "Содержание"])
        section = title
        for block in blocks:
            if block.kind == "heading":
                section = block.text
            elif block.kind in {"paragraph", "bullet", "number", "code"}:
                sheet.append([_safe(section), _safe(block.text)])
        _style_sheet(sheet)
    workbook.properties.title = title[:255]
    workbook.properties.creator = "CoreTeams"
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()
