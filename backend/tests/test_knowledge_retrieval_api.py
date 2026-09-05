from __future__ import annotations

import hashlib

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.knowledge_retrieval import (
    KnowledgeReindexRequest,
    KnowledgeRetrievalConfigRequest,
    get_index_status,
    reindex_knowledge,
    upsert_retrieval_config,
)
from app.db.models import (
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeChunk,
    KnowledgeChunkEmbedding,
    KnowledgeDocument,
    KnowledgeRetrievalConfig,
    Tenant,
    User,
)
from app.security.encryption import decrypt_secret


def _db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.commit()
    return db


def _admin() -> User:
    return User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash="unused",
    )


def _config_request(**overrides: object) -> KnowledgeRetrievalConfigRequest:
    values: dict[str, object] = {
        "tenant_id": "tenant_demo",
        "name": "本地混合检索",
        "embedding_base_url": "https://embedding.example/v1",
        "embedding_api_key": "secret-key",
        "embedding_model": "text-embedding-model",
        "embedding_dimensions": 3,
        "reranker_mode": "llm",
        "reranker_model_config_id": None,
        "candidate_limit": 40,
        "rerank_limit": 12,
        "enabled": True,
    }
    values.update(overrides)
    return KnowledgeRetrievalConfigRequest.model_validate(values)


def test_retrieval_config_never_returns_embedding_key() -> None:
    with _db() as db:
        response = upsert_retrieval_config(_config_request(), db=db, current_user=_admin())

        assert response.embedding_api_key_masked.endswith("-key")
        assert "secret-key" not in response.model_dump_json()
        config = db.exec(select(KnowledgeRetrievalConfig)).one()
        assert decrypt_secret(config.embedding_api_key_encrypted) == "secret-key"

        preserved = upsert_retrieval_config(
            _config_request(name="已更新配置", embedding_api_key=None),
            db=db,
            current_user=_admin(),
        )
        assert preserved.embedding_api_key_masked == response.embedding_api_key_masked
        assert decrypt_secret(config.embedding_api_key_encrypted) == "secret-key"


def test_index_status_counts_only_current_content_hash() -> None:
    with _db() as db:
        knowledge_base = KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="能源知识库")
        version = KnowledgeBaseVersion(
            id="kbver_demo",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            version="1.0.0",
            name="能源知识库 1.0.0",
        )
        document = KnowledgeDocument(
            id="kdoc_demo",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            filename="energy.md",
            file_type="md",
            title="能源评审",
            status="ready",
        )
        chunks = [
            KnowledgeChunk(
                id=f"chunk_{index}",
                tenant_id="tenant_demo",
                knowledge_base_id=knowledge_base.id,
                knowledge_base_version_id=version.id,
                document_id=document.id,
                bucket_id="bucket_demo",
                chunk_index=index,
                content=content,
            )
            for index, content in enumerate(["ready", "changed", "failed"])
        ]
        db.add_all([knowledge_base, version, document, *chunks])
        db.commit()
        db.add(
            KnowledgeRetrievalConfig(
                id="retrieval_demo",
                tenant_id="tenant_demo",
                name="Hybrid",
                embedding_base_url="https://embedding.example/v1",
                embedding_api_key_encrypted="",
                embedding_model="text-embedding-model",
                embedding_dimensions=3,
                enabled=True,
            )
        )
        db.add_all(
            [
                KnowledgeChunkEmbedding(
                    tenant_id="tenant_demo",
                    knowledge_base_id=knowledge_base.id,
                    knowledge_base_version_id=version.id,
                    chunk_id=chunks[0].id,
                    retrieval_config_id="retrieval_demo",
                    embedding_model="text-embedding-model",
                    content_sha256=hashlib.sha256(b"ready").hexdigest(),
                    dimensions=3,
                    vector_json=[1.0, 0.0, 0.0],
                ),
                KnowledgeChunkEmbedding(
                    tenant_id="tenant_demo",
                    knowledge_base_id=knowledge_base.id,
                    knowledge_base_version_id=version.id,
                    chunk_id=chunks[1].id,
                    retrieval_config_id="retrieval_demo",
                    embedding_model="text-embedding-model",
                    content_sha256=hashlib.sha256(b"old").hexdigest(),
                    dimensions=3,
                    vector_json=[1.0, 0.0, 0.0],
                ),
                KnowledgeChunkEmbedding(
                    tenant_id="tenant_demo",
                    knowledge_base_id=knowledge_base.id,
                    knowledge_base_version_id=version.id,
                    chunk_id=chunks[2].id,
                    retrieval_config_id="retrieval_demo",
                    embedding_model="text-embedding-model",
                    content_sha256=hashlib.sha256(b"failed").hexdigest(),
                    dimensions=3,
                    vector_json=[],
                    status="failed",
                    error_code="EMBEDDING_PROVIDER_UNAVAILABLE",
                ),
            ]
        )
        db.commit()

        statuses = get_index_status("tenant_demo", db=db)

        assert statuses[0].knowledge_base_version_id == version.id
        assert statuses[0].total_chunks == 3
        assert statuses[0].ready_embeddings == 1
        assert statuses[0].failed_embeddings == 1
        assert statuses[0].missing_embeddings == 1


def test_reindex_queues_only_documents_with_missing_or_changed_chunks(monkeypatch) -> None:
    with _db() as db:
        knowledge_base = KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="能源知识库")
        version = KnowledgeBaseVersion(
            id="kbver_demo",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            version="1.0.0",
            name="能源知识库 1.0.0",
        )
        ready_document = KnowledgeDocument(
            id="kdoc_ready",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            filename="ready.md",
            file_type="md",
            title="已完成",
            status="ready",
        )
        changed_document = KnowledgeDocument(
            id="kdoc_changed",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            filename="changed.md",
            file_type="md",
            title="需更新",
            status="ready",
        )
        ready_chunk = KnowledgeChunk(
            id="chunk_ready",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            document_id=ready_document.id,
            bucket_id="bucket_demo",
            chunk_index=0,
            content="stable",
        )
        changed_chunk = KnowledgeChunk(
            id="chunk_changed",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            document_id=changed_document.id,
            bucket_id="bucket_demo",
            chunk_index=0,
            content="new content",
        )
        db.add_all(
            [
                knowledge_base,
                version,
                ready_document,
                changed_document,
                ready_chunk,
                changed_chunk,
                KnowledgeRetrievalConfig(
                    id="retrieval_demo",
                    tenant_id="tenant_demo",
                    name="Hybrid",
                    embedding_base_url="https://embedding.example/v1",
                    embedding_api_key_encrypted="",
                    embedding_model="text-embedding-model",
                    embedding_dimensions=3,
                    enabled=True,
                ),
                KnowledgeChunkEmbedding(
                    tenant_id="tenant_demo",
                    knowledge_base_id=knowledge_base.id,
                    knowledge_base_version_id=version.id,
                    chunk_id=ready_chunk.id,
                    retrieval_config_id="retrieval_demo",
                    embedding_model="text-embedding-model",
                    content_sha256=hashlib.sha256(b"stable").hexdigest(),
                    dimensions=3,
                    vector_json=[1.0, 0.0, 0.0],
                ),
            ]
        )
        db.commit()
        enqueued: list[dict[str, object]] = []

        def fake_enqueue(_name, _func, *args, metadata=None, **_kwargs):
            enqueued.append({"args": args, "metadata": metadata})
            return type("Job", (), {"id": "job-1"})()

        monkeypatch.setattr("app.api.knowledge_retrieval.enqueue_async_job", fake_enqueue)

        response = reindex_knowledge(
            KnowledgeReindexRequest(
                tenant_id="tenant_demo",
                knowledge_base_version_id=version.id,
            ),
            db=db,
            current_user=_admin(),
        )

        assert response["queued_document_ids"] == [changed_document.id]
        assert len(enqueued) == 1
