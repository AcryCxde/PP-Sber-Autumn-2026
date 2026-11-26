from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Block:
    kind: str
    text: str = ""
    level: int = 0
    rows: tuple[tuple[str, ...], ...] = field(default_factory=tuple)


def _table_row(line: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in line.strip().strip("|").split("|"))


def _separator(line: str) -> bool:
    cells = _table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def plain(text: str) -> str:
    return re.sub(r"(`([^`]+)`|\*\*([^*]+)\*\*)", lambda match: match.group(2) or match.group(3) or "", text)


def parse_markdown(markdown: str) -> list[Block]:
    if not markdown.strip() or "\x00" in markdown:
        raise ValueError("Canonical document must be non-empty UTF-8 text")
    lines = markdown.splitlines()
    blocks: list[Block] = []
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
            blocks.append(Block("code", "\n".join(code)))
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and _separator(lines[index + 1]):
            rows = [_table_row(stripped)]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_table_row(lines[index]))
                index += 1
            blocks.append(Block("table", rows=tuple(rows)))
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            blocks.append(Block("heading", plain(heading.group(2)), len(heading.group(1))))
            index += 1
            continue
        bullet = re.match(r"^-\s+(.+)$", stripped)
        numbered = re.match(r"^\d+\.\s+(.+)$", stripped)
        if bullet or numbered:
            blocks.append(Block("bullet" if bullet else "number", plain((bullet or numbered).group(1))))
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
        blocks.append(Block("paragraph", plain(" ".join(body))))
    return blocks
