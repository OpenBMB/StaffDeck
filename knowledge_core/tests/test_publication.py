from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from knowledge_core.api import create_app
from knowledge_core.configuration import Configuration
from knowledge_core.identity import Principal
from knowledge_core.kernel import storage as s
from knowledge_core.runtime import Runtime


def add_suggestion(runtime, identifier, status="pending"):
    discovery = runtime.plugins["discovery"]
    with runtime.engine.begin() as db:
        db.execute(discovery.suggestions.insert().values(id=identifier, document_id="",
            suggestion_type="procedure", title="Review", status=status, payload={"steps": ["Do work"]},
            source_refs=[], created_at=s.now(), updated_at=s.now()))
        db.execute(discovery.contexts.insert().values(suggestion_id=identifier, context={"tenant_id": "local"}))


def test_review_without_publisher_is_local_and_publish_route_is_absent(tmp_path):
    runtime = Runtime(f"sqlite:///{tmp_path / 'review.sqlite'}",
        configuration=Configuration(modules={"discovery": True, "publication": True}))
    with TestClient(create_app(runtime)) as client:
        add_suggestion(runtime, "review")
        reviewed = client.post("/api/km/v1/suggestions/review/review")
        assert reviewed.status_code == 200
        assert reviewed.json() == {"status": "reviewed", "published": False}
        assert client.post("/api/km/v1/suggestions/review/export").json()["status"] == "reviewed"
        assert client.post("/api/km/v1/suggestions/review/publish").status_code == 404
        assert "publish_discovery" not in client.get("/api/km/config").json()["operations"]


def test_recoverable_publication_reuses_key_and_external_resource(tmp_path):
    class Publisher:
        def __init__(self):
            self.references = {}
            self.fail_once = True

        def publish(self, suggestion, idempotency_key):
            assert suggestion["tenant_id"] == "local"
            assert suggestion["payload_json"] == {"steps": ["Do work"]}
            self.references.setdefault(idempotency_key, {"target_id": "external-one"})
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("external succeeded, response lost")
            return self.references[idempotency_key]

    publisher = Publisher()
    runtime = Runtime(f"sqlite:///{tmp_path / 'publication.sqlite'}", publisher=publisher,
        configuration=Configuration(modules={"discovery": True, "publication": True}))
    with TestClient(create_app(runtime)) as client:
        add_suggestion(runtime, "publish", "reviewed")
        assert client.post("/api/km/v1/suggestions/publish/publish").status_code == 502
        receipt = runtime.plugins["publication"].receipts
        with runtime.engine.connect() as db:
            row = db.execute(select(receipt)).mappings().one()
            assert row["status"] == "retryable" and "response lost" in row["error"]
            key = row["idempotency_key"]
        for _ in range(2):
            response = client.post("/api/km/v1/suggestions/publish/publish")
            assert response.status_code == 200 and response.json() == {"target_id": "external-one"}
        assert list(publisher.references) == [key]
        assert client.post("/api/km/v1/suggestions/publish/reject").status_code == 409


def test_publication_lease_blocks_concurrency_and_recovers(tmp_path):
    class Publisher:
        def publish(self, suggestion, idempotency_key):
            return {"target_id": idempotency_key}

    runtime = Runtime(f"sqlite:///{tmp_path / 'lease.sqlite'}", publisher=Publisher(),
        configuration=Configuration(modules={"discovery": True, "publication": True}))
    runtime.initialize()
    try:
        add_suggestion(runtime, "lease", "reviewed")
        plugin = runtime.plugins["publication"]
        with runtime.engine.begin() as db:
            db.execute(plugin.receipts.insert().values(suggestion_id="lease", idempotency_key="stable-key",
                status="publishing", updated_at=s.now()))
        with pytest.raises(HTTPException) as failure:
            plugin.publish("lease", {"tenant_id": "local"}, Principal("local"))
        assert failure.value.status_code == 409
        with runtime.engine.begin() as db:
            db.execute(plugin.receipts.update().values(updated_at=s.now() - timedelta(minutes=2)))
        assert plugin.publish("lease", {"tenant_id": "local"}, Principal("local")) == {"target_id": "stable-key"}
    finally:
        runtime.close()


def test_historical_confirmed_suggestion_without_receipt_is_never_republished(tmp_path):
    class Publisher:
        def publish(self, *args):
            raise AssertionError("Historical suggestion must never create another resource")

    runtime = Runtime(f"sqlite:///{tmp_path / 'historical.sqlite'}", publisher=Publisher(),
        configuration=Configuration(modules={"discovery": True, "publication": True}))
    with TestClient(create_app(runtime)) as client:
        add_suggestion(runtime, "historical", "confirmed")
        response = client.post("/api/km/v1/suggestions/historical/publish")
        assert response.status_code == 409 and "unknown" in response.json()["detail"]
