from __future__ import annotations

import math
from collections import Counter
from time import perf_counter

from app.db.models import KnowledgeChunk
from app.knowledge.retrieval.contracts import RetrievalCandidate, RetrievalResult
from app.knowledge.retrieval.options import BM25Options
from app.knowledge.retrieval.tokenizer import retrieval_terms


class BM25Retriever:
    def __init__(self, options: BM25Options | None = None, *, k1: float | None = None, b: float | None = None) -> None:
        self.options = options or BM25Options(
            k1=k1 if k1 is not None else 1.5,
            b=b if b is not None else 0.75,
        )

    def retrieve(
        self, query: str, chunks: list[KnowledgeChunk], limit: int
    ) -> RetrievalResult:
        started = perf_counter()
        limit = min(limit, self.options.candidate_limit)
        if not self.options.enabled or limit < 1 or not chunks:
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

        stopwords = {word.lower() for word in self.options.stopwords} if self.options.stopwords_enabled else None
        term_kwargs = {
            "cjk_ngram": self.options.cjk_ngram,
            "preserve_identifiers": self.options.preserve_identifiers,
            "stopwords": stopwords,
        }
        query_terms = retrieval_terms(
            query,
            deduplicate=self.options.deduplicate_query_terms,
            **term_kwargs,
        )
        document_count = len(chunks)
        field_documents = [
            [
                retrieval_terms(field_value, **term_kwargs) if field_value else []
                for field_value, _field_weight in (self._fields(chunk))
            ]
            for chunk in chunks
        ]
        scored: list[tuple[KnowledgeChunk, float]] = []
        for chunk_index, chunk in enumerate(chunks):
            score = 0.0
            for field_index, (field_value, field_weight) in enumerate(self._fields(chunk)):
                if field_weight <= 0 or not field_value or not field_documents[chunk_index][field_index]:
                    continue
                terms = field_documents[chunk_index][field_index]
                document_terms = [
                    fields[field_index]
                    for fields in field_documents
                    if fields[field_index]
                ]
                document_lengths = [len(other_terms) for other_terms in document_terms]
                average_length = sum(document_lengths) / len(document_lengths)
                document_frequency = Counter(
                    term for other_terms in document_terms for term in set(other_terms)
                )
                term_frequency = Counter(terms)
                field_score = 0.0
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
                    denominator = frequency + self.options.k1 * (
                        1 - self.options.b + self.options.b * len(terms) / average_length
                    )
                    field_score += (
                        inverse_document_frequency
                        * frequency
                        * (self.options.k1 + 1)
                        / denominator
                    )
                score += field_weight * field_score
            if score > self.options.minimum_score:
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

    def _fields(self, chunk: KnowledgeChunk) -> list[tuple[str, float]]:
        return [
            (chunk.content, self.options.content_weight),
            (chunk.summary or "", self.options.summary_weight),
            (chunk.source_ref or "", self.options.source_ref_weight),
        ]


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1_000, 3)
