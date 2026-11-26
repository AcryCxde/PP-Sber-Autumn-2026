from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


class DocxRenderError(ValueError):
    pass


def _inline(paragraph, text: str) -> None:
    for part in re.split(r"(`[^`]+`|\*\*[^*]+\*\*)", text):
        if not part:
            continue
        if part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            run.font.name = "DejaVu Sans Mono"
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(45, 73, 82)
        elif part.startswith("**") and part.endswith("**"):
            paragraph.add_run(part[2:-2]).bold = True
        else:
            paragraph.add_run(part)


def _shade(cell, fill: str) -> None:
    props = cell._tc.get_or_add_tcPr()
    shade = OxmlElement("w:shd")
    shade.set(qn("w:fill"), fill)
    props.append(shade)


def _table_row(line: str) -> list[str]:
    return [part.strip() for part in line.strip().strip("|").split("|")]


def _separator(line: str) -> bool:
    cells = _table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _add_table(document: Document, rows: list[list[str]]) -> None:
    width = max(len(row) for row in rows)
    table = document.add_table(rows=0, cols=width)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        for index, cell in enumerate(cells):
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if row_index == 0:
                _shade(cell, "E8EEF9")
            _inline(cell.paragraphs[0], values[index] if index < len(values) else "")
            if row_index == 0:
                for run in cell.paragraphs[0].runs:
                    run.bold = True


def render_docx(markdown: str, *, title: str) -> bytes:
    if not markdown.strip() or "\x00" in markdown:
        raise DocxRenderError("Canonical document must be non-empty UTF-8 text")
    document = Document()
    section = document.sections[0]
    section.top_margin = Inches(0.7)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.85)
    section.right_margin = Inches(0.85)

    document.core_properties.title = title[:255]
    document.core_properties.subject = "CoreTeams exported result"
    document.core_properties.author = "CoreTeams"
    normal = document.styles["Normal"]
    normal.font.name = "DejaVu Sans"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(5)
    for level, size in ((1, 22), (2, 16), (3, 12)):
        style = document.styles[f"Heading {level}"]
        style.font.name = "DejaVu Sans"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(20, 43, 53) if level == 1 else RGBColor(36, 89, 232)

    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("```"):
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            paragraph = document.add_paragraph()
            props = paragraph._p.get_or_add_pPr()
            shade = OxmlElement("w:shd")
            shade.set(qn("w:fill"), "F1F4F3")
            props.append(shade)
            run = paragraph.add_run("\n".join(code))
            run.font.name = "DejaVu Sans Mono"
            run.font.size = Pt(8.5)
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and _separator(lines[index + 1]):
            rows = [_table_row(stripped)]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_table_row(lines[index]))
                index += 1
            _add_table(document, rows)
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            paragraph = document.add_paragraph(style=f"Heading {len(heading.group(1))}")
            _inline(paragraph, heading.group(2))
            index += 1
            continue
        bullet = re.match(r"^-\s+(.+)$", stripped)
        numbered = re.match(r"^\d+\.\s+(.+)$", stripped)
        if bullet or numbered:
            paragraph = document.add_paragraph(style="List Bullet" if bullet else "List Number")
            _inline(paragraph, (bullet or numbered).group(1))
            index += 1
            continue
        body = [stripped]
        index += 1
        while index < len(lines):
            candidate = lines[index].strip()
            if not candidate:
                index += 1
                break
            if re.match(r"^(#{1,3})\s+", candidate) or re.match(r"^-\s+", candidate) or re.match(r"^\d+\.\s+", candidate) or candidate.startswith("```"):
                break
            if candidate.startswith("|") and index + 1 < len(lines) and _separator(lines[index + 1]):
                break
            body.append(candidate)
            index += 1
        paragraph = document.add_paragraph()
        _inline(paragraph, " ".join(body))

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    footer.add_run("CoreTeams · проверенный экспорт")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()
