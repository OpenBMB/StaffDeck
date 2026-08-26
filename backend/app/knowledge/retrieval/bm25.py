from __future__ import annotations

import math
from collections import Counter
from time import perf_counter

from app.db.models import KnowledgeChunk
from app.knowledge.retrieval.contracts import RetrievalCandidate, RetrievalResult
from app.knowledge.retrieval.tokenizer import retrieval_terms


class BM25Retriever:
    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b

    def retrieve(
        self, query: str, chunks: list[KnowledgeChunk], limit: int
    ) -> RetrievalResult:
        started = perf_counter()
        if limit < 1 or not chunks:
            return RetrievalResult(
                candidates=[],
                trace=[
                    {
                        "strategy": "bm25",
                        "candidate_count": 0,
                        "selected_count": 0,
                        "elapsed_ms": _elapsed_ms(started),
                    }
                ],
            )

        query_terms = retrieval_terms(query)
        document_terms = [retrieval_terms(chunk.content) for chunk in chunks]
        document_lengths = [len(terms) for terms in document_terms]
        average_length = sum(document_lengths) / len(document_lengths)
        document_frequency = Counter(
            term for terms in document_terms for term in set(terms)
        )
        document_count = len(chunks)
        scored: list[tuple[KnowledgeChunk, float]] = []
        for chunk, terms, document_length in zip(
            chunks, document_terms, document_lengths, strict=True
        ):
            term_frequency = Counter(terms)
            score = 0.0
            for term in query_terms:
                frequency = term_frequency.get(term, 0)
                if not frequency:
                    continue
                frequency_in_documents = document_frequency[term]
                inverse_document_frequency = math.log(
                    1
                    + (document_count - frequency_in_documents + 0.5)
                    / (frequency_in_documents + 0.5)
                )
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * document_length / average_length
                )
                score += inverse_document_frequency * frequency * (self.k1 + 1) / denominator
            if score > 0:
                scored.append((chunk, score))

        scored.sort(key=lambda item: (-item[1], item[0].chunk_index, item[0].id))
        selected = scored[:limit]
        candidates = [
            RetrievalCandidate(chunk=chunk, score=score, source="bm25", rank=index)
            for index, (chunk, score) in enumerate(selected, start=1)
        ]
        return RetrievalResult(
            candidates=candidates,
            trace=[
                {
                    "strategy": "bm25",
                    "candidate_count": len(scored),
                    "selected_count": len(candidates),
                    "elapsed_ms": _elapsed_ms(started),
                }
            ],
        )


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1_000, 3)
