from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text

from app.api.model_configs import validate_model_token_budget
from app.db.database import _migrate_model_context_budget
from app.db.models import KnowledgeChunk, KnowledgeChunkEmbedding
from app.knowledge.retrieval.bm25 import BM25Retriever


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
