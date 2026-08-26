from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from app.api.model_configs import validate_model_token_budget
from app.db.database import _migrate_model_context_budget
from app.db.models import KnowledgeChunk, KnowledgeChunkEmbedding, KnowledgeRetrievalConfig
from app.knowledge.retrieval.bm25 import BM25Retriever
from app.knowledge.retrieval.contracts import RetrievalCandidate
from app.knowledge.retrieval.indexer import KnowledgeVectorIndexer
from app.knowledge.retrieval.reranker import LLMReranker
from app.knowledge.retrieval.vector import (
    EmbeddingError,
    OpenAICompatibleEmbeddingProvider,
    VectorRetriever,
)
from app.security.encryption import encrypt_secret


def _chunk(chunk_id: str, content: str) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=chunk_id,
        tenant_id="tenant_demo",
        knowledge_base_id="kb-1",
        knowledge_base_version_id="kbver-1",
        document_id=f"doc-{chunk_id}",
        bucket_id=f"bucket-{chunk_id}",
        chunk_index=int(chunk_id.removeprefix("c")),
        content=content,
        source_ref=f"{chunk_id}.md#chunk=0",
    )


def _candidate(chunk_id: str, score: float = 0.5) -> RetrievalCandidate:
    return RetrievalCandidate(_chunk(chunk_id, f"内容 {chunk_id}"), score, "rrf", 1)


class _FakeLLMClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.payload: dict[str, object] | None = None

    def generate_json(self, _prompt: str, payload: dict[str, object]) -> dict[str, object]:
        self.payload = payload
        return self.response


def _retrieval_config() -> KnowledgeRetrievalConfig:
    return KnowledgeRetrievalConfig(
        id="retrieval-1",
        tenant_id="tenant_demo",
        name="Hybrid",
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key_encrypted=encrypt_secret("embedding-secret"),
        embedding_model="text-embedding-model",
        embedding_dimensions=3,
    )


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


class _RecordingEmbeddingProvider:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.inputs.extend(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]


class _SecondBatchFailsEmbeddingProvider(_RecordingEmbeddingProvider):
    def embed(self, texts: list[str]) -> list[list[float]]:
        self.inputs.extend(texts)
        if len(self.inputs) > 32:
            raise EmbeddingError("EMBEDDING_PROVIDER_UNAVAILABLE")
        return [[1.0, 0.0, 0.0] for _ in texts]


def _indexer_with_existing_hash(
    existing_hash: str,
) -> tuple[KnowledgeVectorIndexer, _RecordingEmbeddingProvider]:
    session = _test_session()
    config = _retrieval_config()
    session.add(config)
    session.add(
        KnowledgeChunkEmbedding(
            tenant_id="tenant_demo",
            knowledge_base_id="kb-1",
            knowledge_base_version_id="kbver-1",
            chunk_id="c1",
            retrieval_config_id=config.id,
            embedding_model=config.embedding_model,
            content_sha256=existing_hash,
            dimensions=3,
            vector_json=[1.0, 0.0, 0.0],
        )
    )
    session.commit()
    provider = _RecordingEmbeddingProvider()
    return KnowledgeVectorIndexer(session, config, provider), provider


def test_safe_input_budget_must_leave_output_and_system_reserve() -> None:
    with pytest.raises(HTTPException) as exc:
        validate_model_token_budget(
            context_window_tokens=64_000,
            safe_input_tokens=60_000,
            max_output_tokens=8_192,
        )
    assert exc.value.detail == "MODEL_TOKEN_BUDGET_INVALID"


def test_non_default_budget_requires_verified_and_attested_context() -> None:
    with pytest.raises(HTTPException) as exc:
        validate_model_token_budget(
            context_window_tokens=128_000,
            context_window_source="unverified",
            trust_status="verified",
            safe_input_tokens=96_000,
            max_output_tokens=8_192,
        )
    assert exc.value.detail == "MODEL_CONTEXT_NOT_ATTESTED"


def test_chunk_embedding_identity_includes_content_hash_and_model() -> None:
    row = KnowledgeChunkEmbedding(
        tenant_id="tenant_demo",
        knowledge_base_id="kb-1",
        knowledge_base_version_id="kbver-1",
        chunk_id="chunk-1",
        retrieval_config_id="retrieval-1",
        embedding_model="text-embedding-model",
        content_sha256="a" * 64,
        dimensions=3,
        vector_json=[0.1, 0.2, 0.3],
    )
    assert row.status == "ready"


def test_legacy_model_config_gets_default_budget_columns() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE model_configs "
                "(id VARCHAR PRIMARY KEY, safe_input_tokens INTEGER)"
            )
        )
        conn.execute(
            text("INSERT INTO model_configs (id, safe_input_tokens) VALUES ('legacy', NULL)")
        )
        _migrate_model_context_budget(conn, {"model_configs"})
        columns = {
            str(row[1]) for row in conn.execute(text("PRAGMA table_info(model_configs)")).all()
        }
        row = conn.execute(
            text(
                "SELECT context_window_source, safe_input_tokens "
                "FROM model_configs WHERE id = 'legacy'"
            )
        ).one()

    assert {"context_window_tokens", "context_window_source", "safe_input_tokens"} <= columns
    assert row == ("default", 32_000)


def test_bm25_retrieves_chinese_phrase_and_exact_standard_number() -> None:
    chunks = [
        _chunk("c1", "组织应识别主要能源使用并确定相关变量"),
        _chunk("c2", "GB/T 23331-2020 要求建立能源评审"),
        _chunk("c3", "员工请假流程与办公管理无关"),
    ]
    result = BM25Retriever().retrieve("GB/T 23331-2020 能源评审", chunks, limit=2)

    assert [item.chunk.id for item in result.candidates] == ["c2", "c1"]
    assert result.trace[0]["strategy"] == "bm25"


def test_embedding_provider_uses_openai_compatible_contract() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )

    config = _retrieval_config()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    vectors = OpenAICompatibleEmbeddingProvider(config, client=client).embed(["能源评审"])

    assert vectors == [[0.1, 0.2, 0.3]]
    assert captured[0].url == "https://embedding.example/v1/embeddings"
    assert captured[0].headers["authorization"] == "Bearer embedding-secret"
    assert json.loads(captured[0].content) == {
        "model": config.embedding_model,
        "input": ["能源评审"],
    }
    assert "embedding-secret" not in captured[0].content.decode("utf-8")


def test_embedding_provider_rejects_dimension_mismatch() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        )

    config = _retrieval_config()
    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(EmbeddingError, match="EMBEDDING_DIMENSION_MISMATCH"):
        OpenAICompatibleEmbeddingProvider(config, client=client).embed(["能源评审"])


def test_vector_index_reembeds_only_changed_content() -> None:
    unchanged = _chunk("c1", "unchanged")
    changed = _chunk("c2", "changed")
    indexer, provider = _indexer_with_existing_hash(
        hashlib.sha256(unchanged.content.encode("utf-8")).hexdigest()
    )

    summary = indexer.index_version("tenant_demo", "kbver-1", [unchanged, changed])

    assert provider.inputs == ["changed"]
    assert summary.skipped == 1
    assert summary.indexed == 1


def test_vector_index_keeps_successful_batches_when_a_later_batch_fails() -> None:
    session = _test_session()
    config = _retrieval_config()
    session.add(config)
    session.commit()
    provider = _SecondBatchFailsEmbeddingProvider()
    indexer = KnowledgeVectorIndexer(session, config, provider)

    summary = indexer.index_version(
        "tenant_demo",
        "kbver-1",
        [_chunk(f"c{index}", f"content {index}") for index in range(33)],
    )
    rows = session.exec(
        select(KnowledgeChunkEmbedding).where(
            KnowledgeChunkEmbedding.retrieval_config_id == config.id
        )
    ).all()

    assert summary.indexed == 32
    assert summary.failed == 1
    assert len([row for row in rows if row.status == "ready"]) == 32
    assert len([row for row in rows if row.status == "failed"]) == 1


def test_vector_retriever_ranks_by_cosine_similarity() -> None:
    session = _test_session()
    config = _retrieval_config()
    session.add(config)
    session.add_all(
        [
            KnowledgeChunkEmbedding(
                tenant_id="tenant_demo",
                knowledge_base_id="kb-1",
                knowledge_base_version_id="kbver-1",
                chunk_id="c1",
                retrieval_config_id=config.id,
                embedding_model=config.embedding_model,
                content_sha256=hashlib.sha256(b"first").hexdigest(),
                dimensions=3,
                vector_json=[1.0, 0.0, 0.0],
            ),
            KnowledgeChunkEmbedding(
                tenant_id="tenant_demo",
                knowledge_base_id="kb-1",
                knowledge_base_version_id="kbver-1",
                chunk_id="c2",
                retrieval_config_id=config.id,
                embedding_model=config.embedding_model,
                content_sha256=hashlib.sha256(b"second").hexdigest(),
                dimensions=3,
                vector_json=[0.0, 1.0, 0.0],
            ),
        ]
    )
    session.commit()

    result = VectorRetriever(session, config).retrieve(
        [0.0, 1.0, 0.0],
        [_chunk("c1", "first"), _chunk("c2", "second")],
        limit=2,
    )

    assert [item.chunk.id for item in result.candidates] == ["c2", "c1"]
    assert all(item.source == "vector" for item in result.candidates)
    assert result.trace[0]["strategy"] == "vector"


def test_reranker_rejects_unknown_candidate_ids() -> None:
    client = _FakeLLMClient({"ranked": [{"chunk_id": "outside", "score": 0.99}]})

    result = LLMReranker(client).rerank(
        "能源绩效",
        [_candidate("c1"), _candidate("c2")],
        limit=2,
    )

    assert [item.chunk.id for item in result.candidates] == ["c1", "c2"]
    assert result.trace[-1]["fallback_reason"] == "RERANK_UNKNOWN_CANDIDATE"


def test_reranker_uses_model_order_and_keeps_source_scores() -> None:
    client = _FakeLLMClient(
        {
            "ranked": [
                {"chunk_id": "c2", "score": 0.9},
                {"chunk_id": "c1", "score": 0.7},
            ]
        }
    )

    result = LLMReranker(client).rerank(
        "能源绩效",
        [_candidate("c1"), _candidate("c2")],
        limit=2,
    )

    assert [item.chunk.id for item in result.candidates] == ["c2", "c1"]
    assert [item.score for item in result.candidates] == [0.9, 0.7]
    assert all(item.source == "reranker" for item in result.candidates)
    assert client.payload == {
        "query": "能源绩效",
        "candidates": [
            {"chunk_id": "c1", "content": "内容 c1"},
            {"chunk_id": "c2", "content": "内容 c2"},
        ],
    }
