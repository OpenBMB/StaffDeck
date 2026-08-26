from __future__ import annotations

from typing import Protocol

from app.db.models import KnowledgeChunk

from .contracts import CandidateRetriever, RetrievalCandidate, RetrievalResult
from .reranker import CandidateReranker
from .vector import EmbeddingError, EmbeddingProvider


class HybridVectorRetriever(Protocol):
    def retrieve(
        self,
        query: str | list[float],
        chunks: list[KnowledgeChunk],
        limit: int,
    ) -> RetrievalResult:
        raise NotImplementedError


class HybridKnowledgeRetriever:
    def __init__(
        self,
        *,
        bm25: CandidateRetriever,
        vector: HybridVectorRetriever,
        reranker: CandidateReranker,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.bm25 = bm25
        self.vector = vector
        self.reranker = reranker
        self.embedding_provider = embedding_provider

    def retrieve(
        self,
        query: str,
        chunks: list[KnowledgeChunk],
        candidate_limit: int,
        rerank_limit: int,
    ) -> RetrievalResult:
        bm25_result = self.bm25.retrieve(query, chunks, candidate_limit)
        trace = list(bm25_result.trace)
        groups = [bm25_result.candidates]

        try:
            if self.embedding_provider is None:
                vector_result = self.vector.retrieve(query, chunks, candidate_limit)
            else:
                query_vectors = self.embedding_provider.embed([query])
                if len(query_vectors) != 1:
                    raise EmbeddingError("EMBEDDING_RESPONSE_INVALID")
                vector_result = self.vector.retrieve(
                    query_vectors[0], chunks, candidate_limit
                )
            groups.append(vector_result.candidates)
            trace.extend(vector_result.trace)
        except Exception as exc:  # noqa: BLE001 - one failed branch must not stop search.
            trace.append(
                {
                    "strategy": "vector",
                    "candidate_count": 0,
                    "selected_count": 0,
                    "fallback_reason": _error_code(exc),
                }
            )

        fused = reciprocal_rank_fusion(groups, limit=candidate_limit)
        trace.append(
            {
                "strategy": "rrf",
                "candidate_count": sum(len(group) for group in groups),
                "selected_count": len(fused),
            }
        )
        try:
            reranked = self.reranker.rerank(query, fused, rerank_limit)
            trace.extend(reranked.trace)
            return RetrievalResult(candidates=reranked.candidates, trace=trace)
        except Exception as exc:  # noqa: BLE001 - strict fallback preserves fused results.
            trace.append(
                {
                    "strategy": "reranker",
                    "selected_count": min(rerank_limit, len(fused)),
                    "fallback_reason": _error_code(exc),
                }
            )
            return RetrievalResult(candidates=fused[:rerank_limit], trace=trace)


class PassthroughReranker:
    def __init__(self, fallback_reason: str = "RERANKER_NOT_CONFIGURED") -> None:
        self.fallback_reason = fallback_reason

    def rerank(
        self,
        _query: str,
        candidates: list[RetrievalCandidate],
        limit: int,
    ) -> RetrievalResult:
        selected = candidates[:limit]
        return RetrievalResult(
            candidates=selected,
            trace=[
                {
                    "strategy": "reranker",
                    "selected_count": len(selected),
                    "fallback_reason": self.fallback_reason,
                }
            ],
        )


def reciprocal_rank_fusion(
    groups: list[list[RetrievalCandidate]],
    *,
    k: int = 60,
    limit: int | None = None,
) -> list[RetrievalCandidate]:
    scores: dict[str, float] = {}
    chunks: dict[str, KnowledgeChunk] = {}
    for group in groups:
        for rank, candidate in enumerate(group, start=1):
            chunks[candidate.chunk.id] = candidate.chunk
            scores[candidate.chunk.id] = scores.get(candidate.chunk.id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    if limit is not None:
        ordered = ordered[: max(0, limit)]
    return [
        RetrievalCandidate(chunks[chunk_id], scores[chunk_id], "rrf", index + 1)
        for index, chunk_id in enumerate(ordered)
    ]


def _error_code(exc: Exception) -> str:
    if exc.args and isinstance(exc.args[0], str):
        return str(exc.args[0])
    return "VECTOR_RETRIEVAL_FAILED"
