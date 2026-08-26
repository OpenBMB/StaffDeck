from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkSpan:
    chunk_index: int
    start_char: int
    end_char: int
    content: str
    content_sha256: str


def chunk_text(text: str, target_chars: int = 1_000) -> list[ChunkSpan]:
    """Split text into deterministic, contiguous character spans.

    The default 900-1,200 character window keeps chunks small enough for
    downstream processing while preserving every character exactly once.
    """

    if not 900 <= target_chars <= 1_200:
        raise ValueError("target_chars must be between 900 and 1200")

    spans: list[ChunkSpan] = []
    start = 0
    while start < len(text):
        preferred_start = min(len(text), start + target_chars - 100)
        hard_end = min(len(text), start + target_chars + 200)
        boundary = text.rfind("\n", preferred_start, hard_end)
        end = boundary + 1 if boundary >= preferred_start else hard_end
        content = text[start:end]
        spans.append(
            ChunkSpan(
                chunk_index=len(spans),
                start_char=start,
                end_char=end,
                content=content,
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
        start = end
    return spans
