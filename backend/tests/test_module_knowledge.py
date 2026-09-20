from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.module_knowledge import router
from app.db import get_session
from app.agents.branching import ensure_open_gallery_binding, mark_resource_open_gallery
from app.db.models import (
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDocument,
    Tenant,
    AgentProfile,
)


def _client() -> tuple[TestClient, Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    app = FastAPI()
    app.include_router(router)

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app), session


def _envelope(operation: str, value: dict, request_id: str = "request-1") -> dict:
    return {
        "kind": "request",
        "method": "module_call",
        "messageId": "message-1",
        "runId": "run-1",
        "operationId": "operation-1",
        "requestId": request_id,
        "module": "knowledge",
        "payload": {"operation": operation, "input": value},
    }


def test_manifest_publishes_the_full_staffdeck_knowledge_contract() -> None:
    client, session = _client()
    try:
        response = client.get("/module-manifest")
        assert response.status_code == 200
        assert response.json() == {
            "protocolVersion": "2.0",
            "implementationId": "staffdeck.knowledge",
            "contract": "staffdeck.knowledge/v1",
            "transport": "module-http-v2",
            "methods": [
                "list_bases",
                "create_base",
                "get_base",
                "update_base",
                "delete_base",
                "list_versions",
                "sync_base",
                "publish_version",
                "rollback_version",
                "list_documents",
                "get_document",
                "import_document",
                "import_okf",
                "update_document",
                "delete_document",
                "list_document_buckets",
                "update_bucket",
                "list_bucket_chunks",
                "update_chunk",
                "get_job",
                "list_jobs",
                "cancel_job",
                "list_okf_concepts",
                "get_okf_concept",
                "upsert_okf_concept",
                "export_okf",
                "lint_okf",
                "list_discoveries",
                "confirm_discovery",
                "reject_discovery",
                "query",
                "resolve_citation",
            ],
            "state": {
                "ownership": "module",
                "scope": "tenant",
                "persistence": "staffdeck-database",
            },
        }
    finally:
        session.close()


def test_resolve_citation_uses_the_persisted_staffdeck_chunk_projection() -> None:
    client, session = _client()
    try:
        session.add(Tenant(id="tenant_demo", name="Demo"))
        document = KnowledgeDocument(
            id="document-1",
            tenant_id="tenant_demo",
            knowledge_base_id="base-1",
            filename="policy.md",
            file_type="md",
            status="ready",
        )
        bucket = KnowledgeBucket(
            id="bucket-1",
            tenant_id="tenant_demo",
            knowledge_base_id="base-1",
            document_id=document.id,
            bucket_key="refunds",
            title="Refunds",
            summary="Refund policy",
        )
        chunk = KnowledgeChunk(
            id="chunk-1",
            tenant_id="tenant_demo",
            knowledge_base_id="base-1",
            document_id=document.id,
            bucket_id=bucket.id,
            chunk_index=0,
            content="Refunds require approval.",
            summary="Approval requirement",
            source_ref="policy.md#refunds",
        )
        session.add(document)
        session.add(bucket)
        session.add(chunk)
        session.commit()

        response = client.post(
            "/v2/module/call",
            json=_envelope(
                "resolve_citation",
                {"tenantId": "tenant_demo", "chunkId": "chunk-1"},
                "citation-request",
            ),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["inReplyTo"] == "message-1"
        assert body["requestId"] == "citation-request"
        assert body["ok"] is True
        assert body["payload"]["result"]["id"] == "chunk-1"
        assert body["payload"]["result"]["content"] == "Refunds require approval."
        assert body["payload"]["result"]["source_ref"] == "policy.md#refunds"
    finally:
        session.close()


def test_okf_management_operations_forward_to_the_native_owner() -> None:
    client, session = _client()
    try:
        session.add(Tenant(id="tenant_demo", name="Demo"))
        session.add(AgentProfile(id="agent-overall", tenant_id="tenant_demo", name="Overall", is_overall=True))
        base = KnowledgeBase(id="base-okf", tenant_id="tenant_demo", name="Approval policy")
        version = KnowledgeBaseVersion(
            id="version-okf",
            tenant_id="tenant_demo",
            knowledge_base_id=base.id,
            version="1.0.0",
            name=base.name,
        )
        concept = KnowledgeConcept(
            id="concept-okf",
            tenant_id="tenant_demo",
            knowledge_base_id=base.id,
            knowledge_base_version_id=version.id,
            concept_id="rules/approval",
            concept_type="Business Rule",
            title="Approval",
            content_md="---\ntype: Business Rule\ntitle: Approval\n---\nApproval is required.",
        )
        session.add(base)
        session.add(version)
        session.add(concept)
        mark_resource_open_gallery(base, {})
        ensure_open_gallery_binding(
            session, "tenant_demo", "knowledge_base", base.id, "active", metadata_json={}
        )
        session.commit()

        concepts = client.post(
            "/v2/module/call",
            json=_envelope("list_okf_concepts", {"tenantId": "tenant_demo", "knowledgeBaseId": base.id}),
        )
        assert concepts.status_code == 200
        assert concepts.json()["payload"]["result"][0]["concept_id"] == "rules/approval"

        lint = client.post(
            "/v2/module/call",
            json=_envelope("lint_okf", {"tenantId": "tenant_demo", "knowledgeBaseId": base.id}),
        )
        assert lint.status_code == 200
        assert lint.json()["payload"]["result"]["status"] == "ok"

        exported = client.post(
            "/v2/module/call",
            json=_envelope("export_okf", {"tenantId": "tenant_demo", "knowledgeBaseId": base.id}),
        )
        assert exported.status_code == 200
        result = exported.json()["payload"]["result"]
        assert result["media_type"] == "application/zip"
        assert result["content_base64"]
    finally:
        session.close()


def test_unknown_operation_returns_a_correlated_protocol_failure() -> None:
    client, session = _client()
    try:
        response = client.post("/v2/module/call", json=_envelope("invented", {}, "bad-request"))
        assert response.status_code == 400
        body = response.json()
        assert body["inReplyTo"] == "message-1"
        assert body["requestId"] == "bad-request"
        assert body["ok"] is False
        assert body["code"] == "MODULE_PROTOCOL_INCOMPATIBLE"
    finally:
        session.close()


def test_malformed_envelope_returns_a_correlated_protocol_failure() -> None:
    client, session = _client()
    try:
        response = client.post(
            "/v2/module/call",
            json={"messageId": "broken-message", "requestId": "broken-request", "payload": {}},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["inReplyTo"] == "broken-message"
        assert body["requestId"] == "broken-request"
        assert body["code"] == "MODULE_PROTOCOL_INCOMPATIBLE"
    finally:
        session.close()
