from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.knowledge_retrieval import (
    KnowledgeRetrievalConfigRequest,
    get_retrieval_capabilities,
    get_retrieval_config,
    test_embedding_connection as run_embedding_connection_test,
    upsert_retrieval_config,
)
from app.db.models import KnowledgeRetrievalConfig, Tenant, User


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


def _draft_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "tenant_id": "tenant_demo",
        "name": "知识库混合检索",
        "embedding": {
            "adapter": "zhipu_embedding",
            "base_url": "https://embedding.example/v1",
            "api_key": "embedding-secret",
            "model": "embedding-3",
            "dimension_mode": "explicit",
            "dimensions": 1024,
            "batch_size": 32,
        },
        "bm25": {"enabled": True, "k1": 1.5, "b": 0.75, "candidate_limit": 40},
        "fusion": {"mode": "hybrid", "rrf_k": 60, "bm25_weight": 1.0, "vector_weight": 1.0},
        "reranker": {
            "mode": "none",
            "candidate_limit": 40,
            "rerank_limit": 12,
        },
        "candidate_limit": 40,
        "rerank_limit": 12,
        "enabled": True,
    }
    for key, value in overrides.items():
        if key == "dimensions":
            payload["embedding"] = {**payload["embedding"], "dimensions": value}
        elif key in {"candidate_limit", "rerank_limit"}:
            payload[key] = value
        else:
            payload[key] = value
    return payload


def _request(**overrides: object) -> KnowledgeRetrievalConfigRequest:
    return KnowledgeRetrievalConfigRequest.model_validate(_draft_payload(**overrides))


def test_capabilities_expose_zhipu_dimensions() -> None:
    with _db() as db:
        response = get_retrieval_capabilities(
            tenant_id="tenant_demo", db=db, current_user=_admin()
        )

    zhipu = next(item for item in response["embedding"] if item["id"] == "zhipu_embedding")
    assert zhipu["fields"]["dimensions"]["options"] == [256, 512, 1024, 2048]


def test_embedding_connection_test_does_not_persist_key(monkeypatch) -> None:
    class _FakeProvider:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr(
        "app.api.knowledge_retrieval.OpenAICompatibleEmbeddingProvider",
        _FakeProvider,
    )
    with _db() as db:
        response = run_embedding_connection_test(
            _request(), db=db, current_user=_admin()
        )
        assert response["dimensions"] == 1024
        assert db.exec(select(KnowledgeRetrievalConfig)).first() is None


def test_config_save_rejects_rerank_limit_above_candidate_limit() -> None:
    with _db() as db:
        try:
            upsert_retrieval_config(
                _request(candidate_limit=10, rerank_limit=12),
                db=db,
                current_user=_admin(),
            )
        except Exception as exc:  # noqa: BLE001 - assert the API's stable error detail.
            assert getattr(exc, "detail", None) == "RERANK_LIMIT_EXCEEDS_CANDIDATE_LIMIT"
        else:
            raise AssertionError("invalid rerank limit must be rejected")


def test_config_save_returns_reindex_requirement_for_embedding_identity_change() -> None:
    with _db() as db:
        first = upsert_retrieval_config(_request(), db=db, current_user=_admin())
        second = upsert_retrieval_config(
            _request(dimensions=512, expected_revision=first.revision),
            db=db,
            current_user=_admin(),
        )

    assert first.status == "active"
    assert second.requires_reindex is True
    assert second.active_config_id == first.id
    assert second.pending_config_id


def test_grouped_options_round_trip_without_returning_secrets() -> None:
    with _db() as db:
        saved = upsert_retrieval_config(_request(), db=db, current_user=_admin())
        read = get_retrieval_config("tenant_demo", db=db)

    assert saved.bm25["k1"] == 1.5
    assert read.reranker_api_key_masked == ""
    assert "embedding-secret" not in read.model_dump_json()
