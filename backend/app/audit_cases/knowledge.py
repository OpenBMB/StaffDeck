from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, Field
from sqlmodel import Session, delete, select

from app.audit_cases.elements import AuditElement, load_required_elements
from app.db.models import AuditCase, AuditElementCoverage, AuditEvidenceLedger, ModelConfig, utc_now
from app.knowledge.schema import KnowledgeChunkRead, KnowledgeSearchRequest, KnowledgeSearchResponse

KNOWLEDGE_RETRIEVAL_VERSION = "knowledge-retrieval-v1"
KnowledgeSearch = Callable[
    [KnowledgeSearchRequest, ModelConfig | None], KnowledgeSearchResponse
]


class KnowledgeRetrievalSummary(BaseModel):
    queried_element_ids: list[str] = Field(default_factory=list)


class AuditKnowledgeOrchestrator:
    def __init__(self, db: Session, search: KnowledgeSearch | None = None) -> None:
        self.db = db
        if search is None:
            from app.knowledge.service import KnowledgeService

            self.search = KnowledgeService(db).search
        else:
            self.search = search

    def retrieve(
        self, case: AuditCase, model_config: ModelConfig | None
    ) -> KnowledgeRetrievalSummary:
        summary = KnowledgeRetrievalSummary()
        for element in load_required_elements(case.management_systems_json):
            hits: dict[str, KnowledgeChunkRead] = {}
            for template in element.query_templates:
                response = self.search(
                    KnowledgeSearchRequest(
                        tenant_id=case.tenant_id,
                        query=template.format(
                            organization_name=case.organization_name,
                            requirement=element.requirement,
                        ),
                        query_type="policy_check",
                        knowledge_base_version_ids=case.knowledge_base_version_ids_json,
                        max_chunks=8,
                    ),
                    model_config,
                )
                for chunk in response.chunks:
                    hits[chunk.id] = chunk
            self._replace_element_knowledge(case, element, list(hits.values()))
            summary.queried_element_ids.append(element.id)
        return summary

    def _replace_element_knowledge(
        self, case: AuditCase, element: AuditElement, chunks: list[KnowledgeChunkRead]
    ) -> None:
        self.db.exec(
            delete(AuditEvidenceLedger).where(
                AuditEvidenceLedger.tenant_id == case.tenant_id,
                AuditEvidenceLedger.audit_case_id == case.id,
                AuditEvidenceLedger.audit_element_id == element.id,
                AuditEvidenceLedger.source_kind == "knowledge",
                AuditEvidenceLedger.extractor_version == KNOWLEDGE_RETRIEVAL_VERSION,
            )
        )
        for chunk in chunks:
            self.db.add(
                AuditEvidenceLedger(
                    tenant_id=case.tenant_id,
                    audit_case_id=case.id,
                    audit_element_id=element.id,
                    source_kind="knowledge",
                    source_id=chunk.document_id,
                    source_version_id=_source_version_id(case, chunk),
                    chunk_id=chunk.id,
                    source_ref=chunk.source_ref or f"{chunk.document_id}#chunk={chunk.chunk_index}",
                    evidence_type="context",
                    evidence_text=chunk.content,
                    confidence=_confidence(chunk),
                    extractor_version=KNOWLEDGE_RETRIEVAL_VERSION,
                )
            )
        coverage = self.db.exec(
            select(AuditElementCoverage).where(
                AuditElementCoverage.tenant_id == case.tenant_id,
                AuditElementCoverage.audit_case_id == case.id,
                AuditElementCoverage.audit_element_id == element.id,
            )
        ).first()
        if coverage is None:
            coverage = AuditElementCoverage(
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                audit_element_id=element.id,
            )
        coverage.knowledge_evidence_count = len(chunks)
        coverage.status = "knowledge_found" if chunks else "evidence_gap"
        coverage.gap_reason = None if chunks else "KNOWLEDGE_ZERO_HIT"
        coverage.updated_at = utc_now()
        self.db.add(coverage)
        self.db.commit()


def _source_version_id(case: AuditCase, chunk: KnowledgeChunkRead) -> str:
    version_id = chunk.metadata.get("knowledge_base_version_id")
    if version_id:
        return str(version_id)
    versions = case.knowledge_base_version_ids_json
    if len(versions) == 1:
        return versions[0]
    return "unversioned"


def _confidence(chunk: KnowledgeChunkRead) -> float:
    raw_score = chunk.metadata.get("score", 1.0)
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return 1.0
    return min(max(score, 0.0), 1.0)
