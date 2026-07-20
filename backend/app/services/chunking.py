"""
Heading-aware Markdown chunker.

Strategy:
1. Split on heading lines (# ... through #### ...)
2. For sections that exceed max_tokens, further split by sentence boundary
3. Apply overlap_tokens sliding window between adjacent chunks
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^#{1,4}\s+", re.MULTILINE)
_APPROX_CHARS_PER_TOKEN = 4  # rough BPE estimate


def _token_count(text: str) -> int:
    return max(1, len(text) // _APPROX_CHARS_PER_TOKEN)


def _split_by_tokens(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split a flat string into token-bounded chunks with overlap."""
    max_chars = max_tokens * _APPROX_CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * _APPROX_CHARS_PER_TOKEN

    if len(text) <= max_chars:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        # Prefer breaking at sentence boundary
        if end < len(text):
            # Look backwards for '. ' or '\n'
            for sep in (". ", "\n", " "):
                idx = text.rfind(sep, start, end)
                if idx != -1 and idx > start:
                    end = idx + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap_chars if overlap_tokens > 0 else end

    return chunks


def chunk_markdown(
    body: str,
    max_tokens: int = 512,
    overlap_tokens: int = 0,
) -> list[str]:
    """
    Split `body` into a list of text chunks for embedding.
    Heading boundaries are preserved as natural split points.
    """
    if not body or not body.strip():
        return []

    # Find all heading positions
    heading_positions = [m.start() for m in _HEADING_RE.finditer(body)]

    if not heading_positions:
        return _split_by_tokens(body, max_tokens, overlap_tokens)

    # Split body at heading boundaries
    sections: list[str] = []
    for i, pos in enumerate(heading_positions):
        end = heading_positions[i + 1] if i + 1 < len(heading_positions) else len(body)
        sections.append(body[pos:end])

    # Pre-heading content (before first heading)
    if heading_positions[0] > 0:
        sections.insert(0, body[: heading_positions[0]])

    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if _token_count(section) <= max_tokens:
            chunks.append(section)
        else:
            chunks.extend(_split_by_tokens(section, max_tokens, overlap_tokens))

    return [c for c in chunks if c]
