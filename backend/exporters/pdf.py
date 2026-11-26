from __future__ import annotations

import io
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, Preformatted, SimpleDocTemplate, Spacer, TableStyle

from backend.exporters.markdown import parse_markdown


def _font() -> str:
    candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            pdfmetrics.registerFont(TTFont("CoreTeamsSans", str(candidate)))
            return "CoreTeamsSans"
    raise RuntimeError("No Cyrillic TrueType font available")


def render_pdf(markdown: str, *, title: str) -> bytes:
    font = _font()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=title[:255],
        author="CoreTeams",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
    )
    base = getSampleStyleSheet()
    styles = {
        "body": ParagraphStyle("Body", parent=base["BodyText"], fontName=font, fontSize=10, leading=14, textColor=colors.HexColor("#1B303A"), spaceAfter=5),
        "h1": ParagraphStyle("H1", parent=base["Heading1"], fontName=font, fontSize=21, leading=25, textColor=colors.HexColor("#142B35"), spaceAfter=10),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontName=font, fontSize=15, leading=19, textColor=colors.HexColor("#2459E8"), spaceBefore=9, spaceAfter=6),
        "h3": ParagraphStyle("H3", parent=base["Heading3"], fontName=font, fontSize=12, leading=15, textColor=colors.HexColor("#2459E8"), spaceBefore=6, spaceAfter=4),
        "code": ParagraphStyle("Code", parent=base["Code"], fontName=font, fontSize=8, leading=11, backColor=colors.HexColor("#F1F4F3"), leftIndent=8, rightIndent=8),
    }
    story = []
    for block in parse_markdown(markdown):
        if block.kind == "heading":
            story.append(Paragraph(block.text, styles[f"h{block.level}"]))
        elif block.kind == "paragraph":
            story.append(Paragraph(block.text, styles["body"]))
        elif block.kind in {"bullet", "number"}:
            prefix = "•" if block.kind == "bullet" else "—"
            story.append(Paragraph(f"{prefix} {block.text}", styles["body"]))
        elif block.kind == "code":
            story.extend([Preformatted(block.text, styles["code"]), Spacer(1, 4)])
        elif block.kind == "table":
            table = LongTable([[Paragraph(cell, styles["body"]) for cell in row] for row in block.rows], repeatRows=1)
            table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF9")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C8D1CF")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.extend([table, Spacer(1, 6)])
    footer = ParagraphStyle("Footer", parent=styles["body"], alignment=TA_CENTER, fontSize=8, textColor=colors.HexColor("#5E7175"))
    story.extend([Spacer(1, 12), Paragraph("CoreTeams · проверенный экспорт", footer)])
    document.build(story)
    return buffer.getvalue()
