from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit_cases import knowledge as knowledge_module
from app.audit_cases.elements import AuditElement
from app.audit_cases.knowledge import AuditKnowledgeOrchestrator
from app.db.models import AuditCase, AuditElementCoverage
from app.knowledge.schema import KnowledgeChunkRead, KnowledgeSearchRequest, KnowledgeSearchResponse


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


class _FakeKnowledgeSearch:
    def __init__(self, hits_by_element: dict[str, list[KnowledgeChunkRead]]) -> None:
        self.hits_by_element = hits_by_element
        self.requests: list[KnowledgeSearchRequest] = []

    def __call__(
        self, request: KnowledgeSearchRequest, _model_config: object
    ) -> KnowledgeSearchResponse:
        self.requests.append(request)
        element_id = request.query.split("|", maxsplit=1)[0]
        return KnowledgeSearchResponse(chunks=self.hits_by_element.get(element_id, []))


def _case() -> AuditCase:
    return AuditCase(
        id="case-knowledge",
        tenant_id="tenant_demo",
        owner_user_id="user-1",
        organization_name="示例组织",
        report_type="再认证审核报告",
        management_systems_json=["GB/T 23331-2020"],
        knowledge_base_version_ids_json=["kb-version-1"],
    )


def _element(element_id: str) -> AuditElement:
    return AuditElement(
        id=element_id,
        management_system="GB/T 23331-2020",
        title=f"条款 {element_id}",
        requirement="检查要求",
        query_templates=[f"{element_id}|请查找 {{organization_name}} 的 {{requirement}}。"],
        report_section_id="audit_findings",
    )


def _knowledge_chunk() -> KnowledgeChunkRead:
    return KnowledgeChunkRead(
        id="knowledge-chunk-1",
        tenant_id="tenant_demo",
        knowledge_base_id="kb-1",
        document_id="document-1",
        bucket_id="bucket-1",
        chunk_index=0,
        content="能源评审程序规定了评审输入、主要能源使用识别和改进机会评价。",
        source_ref="能源评审程序.md#chunk=0",
        metadata={"knowledge_base_version_id": "kb-version-1"},
        created_at="2026-08-26T00:00:00",
        updated_at="2026-08-26T00:00:00",
    )


def _coverage(db: Session, element_id: str) -> AuditElementCoverage:
    return db.exec(
        select(AuditElementCoverage).where(
            AuditElementCoverage.audit_case_id == "case-knowledge",
            AuditElementCoverage.audit_element_id == element_id,
        )
    ).one()


def test_orchestrator_records_hits_and_zero_hits_for_every_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elements = [_element("4.4.3"), _element("4.6.1")]
    monkeypatch.setattr(knowledge_module, "load_required_elements", lambda _systems: elements)
    fake_search = _FakeKnowledgeSearch({"4.4.3": [_knowledge_chunk()], "4.6.1": []})

    with _test_session() as db:
        case = _case()
        db.add(case)
        db.commit()
        summary = AuditKnowledgeOrchestrator(db, search=fake_search).retrieve(case, None)

        assert summary.queried_element_ids == ["4.4.3", "4.6.1"]
        assert len(fake_search.requests) == 2
        assert all(request.knowledge_base_version_ids == ["kb-version-1"] for request in fake_search.requests)
        assert _coverage(db, "4.4.3").knowledge_evidence_count == 1
        assert _coverage(db, "4.6.1").status == "evidence_gap"
        assert _coverage(db, "4.6.1").gap_reason == "KNOWLEDGE_ZERO_HIT"
