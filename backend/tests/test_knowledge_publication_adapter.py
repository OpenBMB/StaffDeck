from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.knowledge_publication import router
from app.db import get_session
from app.db.models import KnowledgePublicationReceipt, Skill, Tool
from concurrent.futures import ThreadPoolExecutor


def test_host_publisher_creates_actual_resources_once(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'host.sqlite'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=[
        Skill.__table__, Tool.__table__, KnowledgePublicationReceipt.__table__,
    ])
    def session():
        with Session(engine) as db:
            yield db
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = session
    monkeypatch.setenv("STAFFDECK_KM_PUBLISHER_BEARER", "publisher-secret")
    with TestClient(app) as client:
        for kind, payload in [
            ("procedure", {"description": "Approval", "steps": ["Collect receipts", "Approve expense"]}),
            ("interface", {"url": "https://example.test/expense", "method": "POST"}),
        ]:
            suggestion = {"id": "suggestion-" + kind, "tenant_id": "tenant",
                          "suggestion_type": kind, "title": kind, "payload_json": payload}
            headers = {"Authorization": "Bearer publisher-secret",
                       "Idempotency-Key": f"km:tenant:{suggestion['id']}"}
            response = client.post("/api/internal/km/publications",
                                   json={"suggestion": suggestion}, headers=headers)
            assert response.status_code == 200, response.text
            again = client.post("/api/internal/km/publications",
                                json={"suggestion": suggestion}, headers=headers)
            assert again.json() == response.json()
            concurrent = {**suggestion, "id": "concurrent-" + kind}
            concurrent_headers = {**headers, "Idempotency-Key": f"km:tenant:{concurrent['id']}"}
            with ThreadPoolExecutor(max_workers=4) as executor:
                responses = list(executor.map(lambda _: client.post(
                    "/api/internal/km/publications", json={"suggestion": concurrent},
                    headers=concurrent_headers,
                ), range(4)))
            assert all(item.status_code == 200 for item in responses), [item.text for item in responses]
            assert all(item.json() == responses[0].json() for item in responses)
        assert client.post("/api/internal/km/publications",
                           json={"suggestion": {}}).status_code == 401
    with Session(engine) as db:
        assert len(db.exec(select(Skill)).all()) == 2
        assert len(db.exec(select(Tool)).all()) == 2
        assert len(db.exec(select(KnowledgePublicationReceipt)).all()) == 4
    engine.dispose()


def test_real_host_creation_survives_core_writeback_failure(tmp_path, monkeypatch):
    import sys
    from datetime import timedelta
    from pathlib import Path

    import pytest

    sys.path.insert(0, str(Path(__file__).parents[2] / "knowledge_core" / "src"))
    from knowledge_core.models import KnowledgeDiscoverySuggestion, Publication, utc_now
    from knowledge_core.publication import publish_suggestion
    from knowledge_core.full_runtime import Runtime

    host_engine = create_engine(f"sqlite:///{tmp_path / 'host.sqlite'}")
    SQLModel.metadata.create_all(host_engine, tables=[
        Skill.__table__, Tool.__table__, KnowledgePublicationReceipt.__table__,
    ])

    def session():
        with Session(host_engine) as db:
            yield db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = session
    monkeypatch.setenv("STAFFDECK_KM_PUBLISHER_BEARER", "publisher-secret")
    runtime = Runtime(f"sqlite:///{tmp_path / 'core.sqlite'}")
    runtime.initialize()
    with TestClient(app) as client:
        class Publisher:
            failed_once = False

            def publish(self, suggestion, idempotency_key):
                response = client.post("/api/internal/km/publications", json={"suggestion": suggestion},
                    headers={"Authorization": "Bearer publisher-secret", "Idempotency-Key": idempotency_key})
                assert response.status_code == 200, response.text
                if not self.failed_once:
                    self.failed_once = True
                    monkeypatch.setattr(core_db, "commit", lambda: (_ for _ in ()).throw(
                        RuntimeError("KM writeback unavailable")))
                return response.json()

        publisher = Publisher()
        with runtime.session() as core_db:
            suggestion = KnowledgeDiscoverySuggestion(id="writeback", tenant_id="tenant",
                knowledge_base_id="kb", document_id="doc", suggestion_type="procedure",
                title="Approval", status="reviewed", payload_json={"steps": ["Approve expense"]})
            core_db.add(suggestion)
            core_db.commit()
            core_db.refresh(suggestion)
            original_commit = core_db.commit
            with pytest.raises(RuntimeError, match="writeback unavailable"):
                publish_suggestion(core_db, suggestion, publisher)
            core_db.rollback()
            monkeypatch.setattr(core_db, "commit", original_commit)
        with runtime.session() as core_db:
            receipt = core_db.exec(select(Publication)).one()
            assert receipt.status == "publishing"
            receipt.updated_at = utc_now() - timedelta(minutes=2)
            core_db.add(receipt)
            core_db.commit()
            suggestion = core_db.get(KnowledgeDiscoverySuggestion, "writeback")
            reference = publish_suggestion(core_db, suggestion, publisher)
            assert suggestion.status == "confirmed"
            assert publish_suggestion(core_db, suggestion, publisher) == reference
        with Session(host_engine) as host_db:
            assert len(host_db.exec(select(Skill)).all()) == 1
            assert len(host_db.exec(select(KnowledgePublicationReceipt)).all()) == 1
    runtime.close()
    host_engine.dispose()
