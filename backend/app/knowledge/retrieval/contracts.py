from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.db.models import KnowledgeChunk


@dataclass(frozen=True)
class RetrievalCandidate:
    chunk: KnowledgeChunk
    score: float
    source: str
    rank: int


@dataclass(frozen=True)
class RetrievalResult:
    candidates: list[RetrievalCandidate]
    trace: list[dict[str, Any]]


class CandidateRetriever(Protocol):
    def retrieve(
        self, query: str, chunks: list[KnowledgeChunk], limit: int
    ) -> RetrievalResult:
        raise NotImplementedError
