"""
Chapter-level chunker — one chunk per chapter heading.
No token splitting. The LLM gets full chapter context.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Chunk

HEADING_PATTERN = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)


def split_markdown(file_path: str | Path) -> list[Chunk]:
    """Split markdown into one Chunk per chapter heading."""
    path = Path(file_path)
    text = path.read_text(encoding="utf-8")
    volume = path.stem
    headings = list(HEADING_PATTERN.finditer(text))
    chunks: list[Chunk] = []

    if not headings:
        chunks.append(Chunk(
            id=Chunk.make_id(volume, "Full Text", 0),
            volume=volume, chapter="Full Text", text=text.strip(),
            token_count=_token_estimate(text), index=0,
        ))
        return chunks

    for i, h in enumerate(headings):
        start = h.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        chapter = h.group().lstrip("#").strip()
        chapter_text = text[start:end].strip()
        chunks.append(Chunk(
            id=Chunk.make_id(volume, chapter, i),
            volume=volume, chapter=chapter, text=chapter_text,
            token_count=_token_estimate(chapter_text), index=i,
        ))

    return chunks


def _token_estimate(text: str) -> int:
    return max(1, int(len(text) / 3.5))
