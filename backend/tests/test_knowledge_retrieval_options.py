from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session

from app.knowledge.retrieval.capabilities import adapter_capabilities
from app.knowledge.retrieval.options import (
    EmbeddingOptions,
    RetrievalOptions,
    embedding_identity_fingerprint,
)
from app.db.database import _migrate_knowledge_retrieval_schema
from app.db.models import KnowledgeRetrievalConfig


def test_zhipu_embedding_defaults_to_supported_1024_dimension() -> None:
    options = EmbeddingOptions.for_adapter("zhipu_embedding", model="embedding-3")
    assert options.dimensions == 1024
    assert options.batch_size == 32
    zhipu = adapter_capabilities()["embedding"]["zhipu_embedding"]
    assert zhipu["limits"]["max_batch_size"] == 64


def test_generic_embedding_defaults_to_auto_dimension() -> None:
    options = EmbeddingOptions.for_adapter("openai_compatible_embedding", model="custom")
    assert options.dimension_mode == "auto"


def test_rerank_limit_cannot_exceed_candidate_limit() -> None:
    with pytest.raises(ValueError, match="RERANK_LIMIT_EXCEEDS_CANDIDATE_LIMIT"):
        RetrievalOptions(candidate_limit=10, rerank_limit=12)


def test_embedding_identity_fingerprint_excludes_retry_settings() -> None:
    first = EmbeddingOptions(model="embedding-3", dimensions=1024, max_retries=2)
    second = first.model_copy(update={"max_retries": 5})
    assert embedding_identity_fingerprint(first) == embedding_identity_fingerprint(second)


def test_embedding_identity_fingerprint_includes_dimensions() -> None:
    first = EmbeddingOptions(model="embedding-3", dimensions=1024)
    second = first.model_copy(update={"dimensions": 512})
    assert embedding_identity_fingerprint(first) != embedding_identity_fingerprint(second)


def test_retrieval_config_has_versioned_runtime_defaults() -> None:
    row = KnowledgeRetrievalConfig(
        tenant_id="tenant_demo",
        name="检索配置",
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key_encrypted="encrypted",
        embedding_model="embedding-3",
        embedding_dimensions=1024,
    )
    assert row.schema_version == 2
    assert row.revision == 1
    assert row.status == "active"


def test_retrieval_schema_migration_adds_option_columns_and_backfills() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE knowledge_retrieval_configs ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR, name VARCHAR, "
                "embedding_base_url VARCHAR, embedding_api_key_encrypted VARCHAR, "
                "embedding_model VARCHAR, embedding_dimensions INTEGER, "
                "reranker_mode VARCHAR, reranker_model_config_id VARCHAR, "
                "candidate_limit INTEGER, rerank_limit INTEGER, enabled BOOLEAN, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_retrieval_configs "
                "(id, tenant_id, name, embedding_base_url, embedding_api_key_encrypted, "
                "embedding_model, embedding_dimensions, reranker_mode, candidate_limit, "
                "rerank_limit, enabled) VALUES "
                "('retrieval-1', 'tenant_demo', '旧配置', 'https://example/v1', 'secret', "
                "'embedding-3', 1536, 'llm', 40, 12, 1)"
            )
        )
        inspector = inspect(connection)
        _migrate_knowledge_retrieval_schema(
            connection, inspector, {"knowledge_retrieval_configs"}
        )
        columns = {
            item["name"] for item in inspect(connection).get_columns("knowledge_retrieval_configs")
        }
        row = connection.execute(
            text(
                "SELECT schema_version, revision, status, bm25_options_json, "
                "fusion_options_json FROM knowledge_retrieval_configs WHERE id = 'retrieval-1'"
            )
        ).one()

    assert {
        "schema_version",
        "revision",
        "status",
        "embedding_options_json",
        "bm25_options_json",
        "fusion_options_json",
        "reranker_options_json",
        "reranker_api_key_encrypted",
    } <= columns
    assert row[0:3] == (2, 1, "active")
    assert row[3] == "{}"
    assert row[4] == "{}"
