from __future__ import annotations

import io
import textwrap

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Pt

from backend.exporters.markdown import parse_markdown


MAX_SLIDE_CHARS = 900


def _chunks(lines: list[str]) -> list[list[str]]:
    result: list[list[str]] = []
    current: list[str] = []
    length = 0
    for line in lines:
        pieces = textwrap.wrap(line, width=110, break_long_words=False) or [""]
        for piece in pieces:
            if current and length + len(piece) > MAX_SLIDE_CHARS:
                result.append(current)
                current, length = [], 0
            current.append(piece)
            length += len(piece)
    if current:
        result.append(current)
    return result or [[]]


def render_pptx(markdown: str, *, title: str) -> bytes:
    blocks = parse_markdown(markdown)
    presentation = Presentation()
    presentation.core_properties.title = title[:255]
    presentation.core_properties.author = "CoreTeams"
    title_slide = presentation.slides.add_slide(presentation.slide_layouts[0])
    title_slide.shapes.title.text = title
    title_slide.placeholders[1].text = "Проверенный экспорт CoreTeams"

    sections: list[tuple[str, list[str]]] = []
    heading = "Результат"
    lines: list[str] = []
    for block in blocks:
        if block.kind == "heading" and block.level <= 2:
            if lines:
                sections.append((heading, lines))
            heading, lines = block.text, []
        elif block.kind == "table":
            lines.extend(" | ".join(row) for row in block.rows)
        elif block.kind in {"paragraph", "bullet", "number", "code", "heading"}:
            prefix = "• " if block.kind in {"bullet", "number"} else ""
            lines.append(prefix + block.text)
    if lines:
        sections.append((heading, lines))

    for section_title, section_lines in sections:
        for index, chunk in enumerate(_chunks(section_lines), start=1):
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = section_title if index == 1 else f"{section_title} · продолжение"
            frame = slide.placeholders[1].text_frame
            frame.clear()
            for item_index, line in enumerate(chunk):
                paragraph = frame.paragraphs[0] if item_index == 0 else frame.add_paragraph()
                paragraph.text = line
                paragraph.font.size = Pt(18)
                paragraph.font.color.rgb = RGBColor(27, 48, 58)
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()
