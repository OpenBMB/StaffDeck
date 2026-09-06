from __future__ import annotations

import json
from pathlib import Path

from app.db.models import KnowledgeChunk
from app.knowledge.retrieval.bm25 import BM25Retriever
from app.knowledge.retrieval.contracts import RetrievalCandidate, RetrievalResult
from app.knowledge.retrieval.hybrid import HybridKnowledgeRetriever

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "knowledge_retrieval_eval.json"


def _chunks(fixture: dict[str, object]) -> list[KnowledgeChunk]:
    rows = fixture["chunks"]
    assert isinstance(rows, list)
    return [
        KnowledgeChunk(
            id=str(row["id"]),
            tenant_id="tenant_eval",
            knowledge_base_id="kb_eval",
            knowledge_base_version_id="version_eval",
            document_id=f"doc-{row['id']}",
            bucket_id=f"bucket-{row['id']}",
            chunk_index=index,
            content=str(row["content"]),
        )
        for index, row in enumerate(rows)
        if isinstance(row, dict)
    ]


class _FixedVectorRetriever:
    def retrieve(
        self,
        query: str,
        chunks: list[KnowledgeChunk],
        limit: int,
    ) -> RetrievalResult:
        # This represents a persisted vector index finding semantic matches that
        # need not share every query token with the source text.
        preferred = {
            "能源评审边界": ["eval-c3", "eval-c1"],
            "供应商合同付款": ["eval-c2"],
            "内审纠正措施": ["eval-c4"],
        }.get(query, [])
        by_id = {chunk.id: chunk for chunk in chunks}
        candidates = [
            RetrievalCandidate(by_id[chunk_id], 1.0, "vector", index)
            for index, chunk_id in enumerate(preferred, start=1)
            if chunk_id in by_id
        ][:limit]
        return RetrievalResult(
            candidates=candidates,
            trace=[{"strategy": "vector", "selected_count": len(candidates)}],
        )


class _CountingReranker:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def rerank(
        self,
        query: str,
        candidates: list[RetrievalCandidate],
        limit: int,
    ) -> RetrievalResult:
        self.queries.append(query)
        return RetrievalResult(
            candidates=candidates[:limit],
            trace=[
                {
                    "strategy": "reranker",
                    "selected_count": min(limit, len(candidates)),
                }
            ],
        )


def _recall_at_k(
    result: RetrievalResult, relevant_ids: set[str], k: int
) -> float:
    selected = {candidate.chunk.id for candidate in result.candidates[:k]}
    return float(bool(selected & relevant_ids))


def test_fixed_eval_hybrid_recall_citations_and_reranker_budget() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    chunks = _chunks(fixture)
    valid_chunk_ids = {chunk.id for chunk in chunks}
    legacy = BM25Retriever()
    reranker = _CountingReranker()
    hybrid = HybridKnowledgeRetriever(
        bm25=legacy,
        vector=_FixedVectorRetriever(),
        reranker=reranker,
    )

    legacy_recall = 0.0
    hybrid_recall = 0.0
    invalid_citation_count = 0
    queries = fixture["queries"]
    assert isinstance(queries, list)
    for row in queries:
        assert isinstance(row, dict)
        query = str(row["query"])
        relevant_ids = {str(chunk_id) for chunk_id in row["relevant_chunk_ids"]}
        legacy_result = legacy.retrieve(query, chunks, limit=2)
        hybrid_result = hybrid.retrieve(
            query,
            chunks,
            candidate_limit=3,
            rerank_limit=2,
        )
        legacy_recall += _recall_at_k(legacy_result, relevant_ids, 2)
        hybrid_recall += _recall_at_k(hybrid_result, relevant_ids, 2)
        invalid_citation_count += sum(
            candidate.chunk.id not in valid_chunk_ids
            for candidate in hybrid_result.candidates
        )

    assert hybrid_recall >= legacy_recall
    assert invalid_citation_count == 0
    assert len(reranker.queries) == len(queries)
