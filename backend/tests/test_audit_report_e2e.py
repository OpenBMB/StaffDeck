from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit_cases.chunking import chunk_text
from app.audit_cases.coverage import calculate_coverage
from app.audit_cases.evidence import AuditEvidenceProcessor
from app.audit_cases.knowledge import AuditKnowledgeOrchestrator
from app.audit_cases.reporting import AuditReportService
from app.db.models import (
    AuditCase,
    AuditCaseMaterial,
    AuditCaseMaterialChunk,
    AuditEvidenceLedger,
    AuditReportSection,
)
from app.knowledge.schema import KnowledgeChunkRead, KnowledgeSearchRequest, KnowledgeSearchResponse
from app.llm.client import LLMError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "audit_case_40k"


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _fixed_audit_record() -> str:
    seed = "审核记录条目：已查阅能源管理体系运行证据，并记录审核员询问、抽样、现场确认和审核结论。\n"
    repetitions = 40_000 // len(seed) + 1
    return (seed * repetitions)[:40_000]


class _FakeEvidenceClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def generate_json(self, _prompt: str, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(payload)
        chunk_id = str(payload["chunk_id"])
        return {
            "chunk_id": chunk_id,
            "items": [
                {
                    "audit_element_id": "4.4.3",
                    "evidence_type": "conformity",
                    "evidence_text": f"材料块 {chunk_id} 已被审核。",
                    "confidence": 0.8,
                }
            ],
        }


class _FakeKnowledgeSearch:
    def __call__(
        self, request: KnowledgeSearchRequest, _model_config: object
    ) -> KnowledgeSearchResponse:
        return KnowledgeSearchResponse(
            chunks=[
                KnowledgeChunkRead(
                    id=f"knowledge-{request.query[:12]}",
                    tenant_id=request.tenant_id,
                    knowledge_base_id="kb-1",
                    document_id="document-1",
                    bucket_id="bucket-1",
                    chunk_index=0,
                    content="知识库规定：应保留能源评审和能源绩效评价的客观证据。",
                    source_ref="知识库/能源管理要求.md#chunk=0",
                    metadata={"knowledge_base_version_id": "kb-version-1", "score": 0.95},
                    created_at="2026-08-26T00:00:00",
                    updated_at="2026-08-26T00:00:00",
                )
            ]
        )


class _FakeReportClient:
    def __init__(self) -> None:
        self.failed_once = False

    def generate_text(self, _prompt: str, payload: dict[str, object]) -> str:
        if payload["section_id"] == "improvements" and not self.failed_once:
            self.failed_once = True
            raise LLMError("temporary report model failure")
        evidence = payload["evidence"]
        evidence_id = str(evidence[0]["id"])
        return f"本章节仅依据已登记证据编写。[EVIDENCE:{evidence_id}]"


def test_fixed_40k_material_is_processed_and_report_resumes_by_section() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    record_spec = next(
        item for item in manifest["generated_files"] if item["material_type"] == "audit_record"
    )
    full_text = _fixed_audit_record()
    assert len(full_text) == 40_000
    assert len(full_text) >= record_spec["minimum_characters"]

    with _test_session() as db:
        case = AuditCase(
            id="case-40k",
            tenant_id="tenant_demo",
            owner_user_id="user-1",
            organization_name=manifest["organization_name"],
            report_type=manifest["report_type"],
            management_systems_json=manifest["management_systems"],
            knowledge_base_version_ids_json=["kb-version-1"],
        )
        material = AuditCaseMaterial(
            id="material-40k",
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            attachment_id="attachment-40k",
            material_type="audit_record",
            filename="审核记录.txt",
            content_type="text/plain",
            sha256=hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
            size=len(full_text.encode("utf-8")),
            storage_key="case-40k/material-40k/source",
            characters=len(full_text),
            extraction_status="succeeded",
            processing_status="succeeded",
        )
        chunks = [
            AuditCaseMaterialChunk(
                id=f"chunk-40k-{span.chunk_index}",
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                material_id=material.id,
                chunk_index=span.chunk_index,
                start_char=span.start_char,
                end_char=span.end_char,
                content_sha256=span.content_sha256,
                content=span.content,
                processing_status="pending",
            )
            for span in chunk_text(full_text)
        ]
        db.add(case)
        db.add(material)
        db.add_all(chunks)
        db.commit()

        evidence_client = _FakeEvidenceClient()
        evidence_summary = AuditEvidenceProcessor(
            db, client_factory=lambda _model_config: evidence_client
        ).process_pending_chunks(case, None)
        assert evidence_summary.succeeded == len(chunks)
        assert len(evidence_client.payloads) == len(chunks)
        assert "".join(str(payload["content"]) for payload in evidence_client.payloads) == full_text

        knowledge_summary = AuditKnowledgeOrchestrator(
            db, search=_FakeKnowledgeSearch()
        ).retrieve(case, None)
        assert len(knowledge_summary.queried_element_ids) == 7

        report_client = _FakeReportClient()
        report_service = AuditReportService(
            db, client_factory=lambda _model_config: report_client
        )
        report = report_service.create_version(case)
        first_run = report_service.generate_pending_sections(case, report, None)
        assert first_run.status == "failed"

        second_run = report_service.generate_pending_sections(case, report, None)
        assert second_run.status == "succeeded"
        assert second_run.regenerated_section_ids == ["improvements"]

        coverage = calculate_coverage(db, case)
        assert coverage.file_coverage == 1.0
        assert coverage.chunk_coverage == 1.0
        assert coverage.element_coverage == 1.0

        sections = db.exec(
            select(AuditReportSection).where(AuditReportSection.report_version_id == report.id)
        ).all()
        ledger_ids = {row.id for row in db.exec(select(AuditEvidenceLedger)).all()}
        assert len(sections) == 7
        assert all(section.status == "succeeded" for section in sections)
        assert all(set(section.citation_ids_json) <= ledger_ids for section in sections)
