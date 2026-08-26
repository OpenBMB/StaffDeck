from __future__ import annotations

import hashlib
import math
from time import perf_counter
from typing import Protocol

import httpx
from sqlmodel import Session, select

from app.db.models import KnowledgeChunk, KnowledgeChunkEmbedding, KnowledgeRetrievalConfig
from app.security.encryption import decrypt_secret

from .contracts import RetrievalCandidate, RetrievalResult


class EmbeddingError(RuntimeError):
    """A stable, safe-to-log failure from the embedding provider."""


class VectorIndexError(RuntimeError):
    """A failure while reading or comparing stored vectors."""


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        config: KnowledgeRetrievalConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = config.embedding_base_url.rstrip("/")
        try:
            self.api_key = decrypt_secret(config.embedding_api_key_encrypted)
        except ValueError as exc:
            raise EmbeddingError("EMBEDDING_SECRET_INVALID") from exc
        self.model = config.embedding_model
        self.dimensions = config.embedding_dimensions
        self.client = client or httpx.Client(timeout=120.0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self.client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
            rows = sorted(response.json()["data"], key=lambda item: int(item["index"]))
            vectors = [[float(value) for value in row["embedding"]] for row in rows]
        except EmbeddingError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
            raise EmbeddingError("EMBEDDING_PROVIDER_UNAVAILABLE") from exc

        if len(vectors) != len(texts) or any(
            len(vector) != self.dimensions for vector in vectors
        ):
            raise EmbeddingError("EMBEDDING_DIMENSION_MISMATCH")
        return vectors


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise VectorIndexError("VECTOR_DIMENSION_MISMATCH")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


class VectorRetriever:
    def __init__(self, db: Session, config: KnowledgeRetrievalConfig) -> None:
        self.db = db
        self.config = config

    def retrieve(
        self,
        query_vector: list[float],
        chunks: list[KnowledgeChunk],
        limit: int,
    ) -> RetrievalResult:
        started = perf_counter()
        if limit <= 0 or not chunks:
            return RetrievalResult(
                candidates=[],
                trace=[
                    {
                        "strategy": "vector",
                        "candidate_count": len(chunks),
                        "selected_count": 0,
                        "elapsed_ms": _elapsed_ms(started),
                    }
                ],
            )
        if len(query_vector) != self.config.embedding_dimensions:
            raise VectorIndexError("VECTOR_DIMENSION_MISMATCH")

        chunk_ids = [chunk.id for chunk in chunks]
        rows = self.db.exec(
            select(KnowledgeChunkEmbedding).where(
                KnowledgeChunkEmbedding.tenant_id == self.config.tenant_id,
                KnowledgeChunkEmbedding.retrieval_config_id == self.config.id,
                KnowledgeChunkEmbedding.embedding_model == self.config.embedding_model,
                KnowledgeChunkEmbedding.status == "ready",
                KnowledgeChunkEmbedding.chunk_id.in_(chunk_ids),
            )
        ).all()
        row_by_identity = {
            (row.chunk_id, row.content_sha256): row
            for row in rows
            if row.dimensions == self.config.embedding_dimensions
        }
        scored: list[tuple[KnowledgeChunk, float]] = []
        for chunk in chunks:
            content_sha256 = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
            row = row_by_identity.get((chunk.id, content_sha256))
            if row is None:
                continue
            score = cosine_similarity(query_vector, [float(value) for value in row.vector_json])
            scored.append((chunk, score))

        scored.sort(key=lambda item: (-item[1], item[0].chunk_index, item[0].id))
        selected = [
            RetrievalCandidate(chunk, score, "vector", index + 1)
            for index, (chunk, score) in enumerate(scored[:limit])
        ]
        return RetrievalResult(
            candidates=selected,
            trace=[
                {
                    "strategy": "vector",
                    "candidate_count": len(rows),
                    "selected_count": len(selected),
                    "elapsed_ms": _elapsed_ms(started),
                }
            ],
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))
