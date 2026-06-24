"""
Markdown chunker – splits light novel markdown into token-aware chunks.
Respects chapter/section boundaries where possible.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Chunk

# Approximate: 1 token ≈ 4 characters for English, ~3 for German/JP mixed
CHARS_PER_TOKEN = 3.5
HEADING_PATTERN = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
SCENE_BREAK = re.compile(r"\n\s*\* \* \*\s*\n|\n\s*---+\s*\n|\n\s*◇.*◇\s*\n")


def token_estimate(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def split_markdown(
    file_path: str | Path,
    chunk_tokens: int = 1000,
    overlap_tokens: int = 100,
) -> list[Chunk]:
    """Split a markdown file into Chunk objects with provenance."""

    path = Path(file_path)
    text = path.read_text(encoding="utf-8")
    volume = path.stem  # filename without .md
    chunks: list[Chunk] = []

    # Find chapter boundaries
    headings = list(HEADING_PATTERN.finditer(text))
    chapter = "Preamble"

    for i, h in enumerate(headings):
        start = h.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        chapter = h.group().lstrip("#").strip()
        section_text = text[start:end]

        chunks.extend(
            _chunk_section(
                text=section_text,
                volume=volume,
                chapter=chapter,
                chunk_tokens=chunk_tokens,
                overlap_tokens=overlap_tokens,
                base_index=len(chunks),
            )
        )

    # If no headings found, chunk the whole file
    if not headings:
        chunks = _chunk_section(
            text=text,
            volume=volume,
            chapter="Full Text",
            chunk_tokens=chunk_tokens,
            overlap_tokens=overlap_tokens,
            base_index=0,
        )

    return chunks


def _chunk_section(
    text: str,
    volume: str,
    chapter: str,
    chunk_tokens: int,
    overlap_tokens: int,
    base_index: int,
) -> list[Chunk]:
    """Split a single chapter/section into overlapping chunks."""
    chunks: list[Chunk] = []

    # Respect scene breaks as soft boundaries
    scenes = SCENE_BREAK.split(text)
    current_chunk = ""
    chunk_index = base_index

    for scene in scenes:
        scene = scene.strip()
        if not scene:
            continue

        # If adding this scene exceeds chunk size, finalize current chunk
        if token_estimate(current_chunk + "\n\n" + scene) > chunk_tokens and current_chunk:
            cid = Chunk.make_id(volume, chapter, chunk_index)
            chunks.append(
                Chunk(
                    id=cid,
                    volume=volume,
                    chapter=chapter,
                    text=current_chunk.strip(),
                    token_count=token_estimate(current_chunk),
                    index=chunk_index,
                )
            )
            chunk_index += 1
            # Keep overlap from the end of previous chunk
            overlap_text = _extract_overlap(current_chunk, overlap_tokens)
            current_chunk = overlap_text + "\n\n" + scene if overlap_text else scene
        else:
            current_chunk = (current_chunk + "\n\n" + scene).strip() if current_chunk else scene

    # Don't forget the last chunk
    if current_chunk.strip():
        cid = Chunk.make_id(volume, chapter, chunk_index)
        chunks.append(
            Chunk(
                id=cid,
                volume=volume,
                chapter=chapter,
                text=current_chunk.strip(),
                token_count=token_estimate(current_chunk),
                index=chunk_index,
            )
        )

    return chunks


def _extract_overlap(text: str, overlap_tokens: int) -> str:
    """Extract trailing text as overlap for next chunk."""
    overlap_chars = int(overlap_tokens * CHARS_PER_TOKEN)
    # Try to break at paragraph boundary
    paragraphs = text.split("\n\n")
    overlap = ""
    for p in reversed(paragraphs):
        candidate = (p + "\n\n" + overlap).strip() if overlap else p
        if len(candidate) > overlap_chars:
            break
        overlap = candidate
    return overlap
