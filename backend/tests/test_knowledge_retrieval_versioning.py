from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.knowledge_retrieval import (
    KnowledgeRetrievalConfigActionRequest,
    activate_retrieval_config,
    rollback_retrieval_config,
)
from app.db.models import (
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeChunk,
    KnowledgeChunkEmbedding,
    KnowledgeRetrievalConfig,
    Tenant,
    User,
)
from app.knowledge.service import KnowledgeService


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


def _config(
    config_id: str,
    *,
    status: str,
    enabled: bool,
    dimensions: int = 3,
) -> KnowledgeRetrievalConfig:
    return KnowledgeRetrievalConfig(
        id=config_id,
        tenant_id="tenant_demo",
        name=config_id,
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key_encrypted="",
        embedding_model="embedding-model",
        embedding_dimensions=dimensions,
        status=status,
        enabled=enabled,
        revision=1,
    )


def test_database_config_enables_hybrid_without_environment_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.knowledge.service.get_settings",
        lambda: SimpleNamespace(hybrid_knowledge_retrieval_enabled=False),
    )
    with _db() as db:
        config = _config("old", status="active", enabled=True)
        db.add(config)
        db.commit()

        active = KnowledgeService(db)._active_retrieval_config(config.tenant_id)

    assert active is not None
    assert active.id == config.id


def test_pending_config_is_not_selected_as_active() -> None:
    with _db() as db:
        db.add_all(
            [
                _config("old", status="active", enabled=True, dimensions=1024),
                _config("new", status="pending_index", enabled=False, dimensions=512),
            ]
        )
        db.commit()

        service = KnowledgeService(db)
        assert service._active_retrieval_config("tenant_demo").id == "old"
        assert service._pending_retrieval_config("tenant_demo").id == "new"


def test_failed_pending_rebuild_does_not_switch_active() -> None:
    with _db() as db:
        active = _config("old", status="active", enabled=True)
        pending = _config("new", status="pending_index", enabled=False)
        pending.last_error_code = "EMBEDDING_PROVIDER_UNAVAILABLE"
        pending.status = "failed"
        db.add_all([active, pending])
        db.commit()

        assert KnowledgeService(db)._active_retrieval_config("tenant_demo").id == "old"


def test_ready_pending_config_switches_atomically() -> None:
    with _db() as db:
        knowledge_base = KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="KB")
        version = KnowledgeBaseVersion(
            id="kbver_demo",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            version="1.0.0",
            name="KB 1.0.0",
        )
        chunk = KnowledgeChunk(
            id="chunk_demo",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            document_id="doc_demo",
            bucket_id="bucket_demo",
            chunk_index=0,
            content="能源评审",
        )
        db.add_all(
            [
                knowledge_base,
                version,
                chunk,
                _config("old", status="active", enabled=True),
                _config("new", status="pending_index", enabled=False),
                KnowledgeChunkEmbedding(
                    tenant_id="tenant_demo",
                    knowledge_base_id=knowledge_base.id,
                    knowledge_base_version_id=version.id,
                    chunk_id=chunk.id,
                    retrieval_config_id="new",
                    embedding_model="embedding-model",
                    content_sha256=hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                    dimensions=3,
                    vector_json=[0.1, 0.2, 0.3],
                ),
            ]
        )
        db.commit()

        response = activate_retrieval_config(
            KnowledgeRetrievalConfigActionRequest(
                tenant_id="tenant_demo", config_id="new"
            ),
            db=db,
            current_user=_admin(),
        )

        assert response["status"] == "active"
        assert db.get(KnowledgeRetrievalConfig, "old").status == "archived"
        assert db.get(KnowledgeRetrievalConfig, "new").status == "active"


def test_activation_rejects_pending_config_with_missing_vectors() -> None:
    with _db() as db:
        db.add_all(
            [
                _config("old", status="active", enabled=True),
                _config("new", status="pending_index", enabled=False),
            ]
        )
        db.commit()

        with pytest.raises(Exception) as exc:
            activate_retrieval_config(
                KnowledgeRetrievalConfigActionRequest(
                    tenant_id="tenant_demo", config_id="new"
                ),
                db=db,
                current_user=_admin(),
            )

    assert getattr(exc.value, "detail", None) == "RETRIEVAL_INDEX_NOT_READY"


def test_rollback_restores_previous_archived_config() -> None:
    with _db() as db:
        db.add_all(
            [
                _config("old", status="archived", enabled=False),
                _config("new", status="active", enabled=True),
            ]
        )
        db.commit()

        response = rollback_retrieval_config(
            KnowledgeRetrievalConfigActionRequest(tenant_id="tenant_demo"),
            db=db,
            current_user=_admin(),
        )

    assert response["active_config_id"] == "old"
