from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from sqlmodel import Session, select

from app.db.models import KnowledgeChunk, KnowledgeChunkEmbedding, KnowledgeRetrievalConfig

from .vector import EmbeddingError, EmbeddingProvider

MAX_EMBEDDING_BATCH_SIZE = 32


@dataclass(frozen=True)
class IndexSummary:
    indexed: int = 0
    skipped: int = 0
    failed: int = 0


class KnowledgeVectorIndexer:
    def __init__(
        self,
        db: Session,
        config: KnowledgeRetrievalConfig,
        provider: EmbeddingProvider,
    ) -> None:
        self.db = db
        self.config = config
        self.provider = provider

    def index_version(
        self,
        tenant_id: str,
        knowledge_base_version_id: str,
        chunks: Iterable[KnowledgeChunk],
    ) -> IndexSummary:
        chunk_list = list(chunks)
        if not chunk_list:
            return IndexSummary()
        if tenant_id != self.config.tenant_id:
            raise ValueError("RETRIEVAL_TENANT_MISMATCH")

        existing_rows = self.db.exec(
            select(KnowledgeChunkEmbedding).where(
                KnowledgeChunkEmbedding.tenant_id == tenant_id,
                KnowledgeChunkEmbedding.knowledge_base_version_id == knowledge_base_version_id,
                KnowledgeChunkEmbedding.retrieval_config_id == self.config.id,
                KnowledgeChunkEmbedding.chunk_id.in_([chunk.id for chunk in chunk_list]),
            )
        ).all()
        existing_by_identity = {
            (row.chunk_id, row.content_sha256, row.embedding_model): row for row in existing_rows
        }

        pending: list[tuple[KnowledgeChunk, str]] = []
        skipped = 0
        for chunk in chunk_list:
            content_sha256 = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
            row = existing_by_identity.get(
                (chunk.id, content_sha256, self.config.embedding_model)
            )
            if (
                row is not None
                and row.status == "ready"
                and row.dimensions == self.config.embedding_dimensions
            ):
                skipped += 1
                continue
            pending.append((chunk, content_sha256))

        indexed = 0
        failed = 0
        for batch in _batches(pending, MAX_EMBEDDING_BATCH_SIZE):
            texts = [chunk.content for chunk, _ in batch]
            try:
                vectors = self.provider.embed(texts)
                if len(vectors) != len(batch) or any(
                    len(vector) != self.config.embedding_dimensions for vector in vectors
                ):
                    raise EmbeddingError("EMBEDDING_DIMENSION_MISMATCH")
            except Exception as exc:  # noqa: BLE001 - isolate failures to this batch.
                error_code = _error_code(exc)
                for chunk, content_sha256 in batch:
                    row = existing_by_identity.get(
                        (chunk.id, content_sha256, self.config.embedding_model)
                    )
                    if row is None:
                        row = KnowledgeChunkEmbedding(
                            tenant_id=tenant_id,
                            knowledge_base_id=chunk.knowledge_base_id,
                            knowledge_base_version_id=knowledge_base_version_id,
                            chunk_id=chunk.id,
                            retrieval_config_id=self.config.id,
                            embedding_model=self.config.embedding_model,
                            content_sha256=content_sha256,
                            dimensions=self.config.embedding_dimensions,
                            vector_json=[],
                        )
                        self.db.add(row)
                        existing_by_identity[
                            (chunk.id, content_sha256, self.config.embedding_model)
                        ] = row
                    row.status = "failed"
                    row.error_code = error_code
                self.db.commit()
                failed += len(batch)
                continue

            for (chunk, content_sha256), vector in zip(batch, vectors, strict=True):
                row = existing_by_identity.get(
                    (chunk.id, content_sha256, self.config.embedding_model)
                )
                if row is None:
                    row = KnowledgeChunkEmbedding(
                        tenant_id=tenant_id,
                        knowledge_base_id=chunk.knowledge_base_id,
                        knowledge_base_version_id=knowledge_base_version_id,
                        chunk_id=chunk.id,
                        retrieval_config_id=self.config.id,
                        embedding_model=self.config.embedding_model,
                        content_sha256=content_sha256,
                        dimensions=self.config.embedding_dimensions,
                        vector_json=[],
                    )
                    self.db.add(row)
                    existing_by_identity[
                        (chunk.id, content_sha256, self.config.embedding_model)
                    ] = row
                row.vector_json = [float(value) for value in vector]
                row.status = "ready"
                row.error_code = None
            self.db.commit()
            indexed += len(batch)

        return IndexSummary(indexed=indexed, skipped=skipped, failed=failed)


def _batches(
    items: list[tuple[KnowledgeChunk, str]], size: int
) -> Iterable[list[tuple[KnowledgeChunk, str]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _error_code(exc: Exception) -> str:
    if isinstance(exc, (EmbeddingError, ValueError)) and exc.args and isinstance(exc.args[0], str):
        return str(exc.args[0])
    return "EMBEDDING_INDEX_FAILED"
