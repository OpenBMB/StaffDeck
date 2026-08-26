from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit_cases.elements import load_required_elements
from app.audit_cases.evidence import AuditEvidenceProcessor
from app.audit_cases.evidence_schema import AuditEvidenceError, validate_extraction_result
from app.db.models import (
    AuditCase,
    AuditCaseMaterialChunk,
    AuditElementCoverage,
    AuditEvidenceLedger,
)


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_evidence_and_report_rows_have_stable_idempotency_keys() -> None:
    with _test_session() as db:
        ledger = AuditEvidenceLedger(
            tenant_id="tenant_demo",
            audit_case_id="case-1",
            audit_element_id="4.4.3",
            source_kind="case_material",
            source_id="material-1",
            source_version_id="material-1:v1",
            chunk_id="chunk-1",
            source_ref="审核记录.pdf#chunk=0",
            evidence_type="conformity",
            evidence_text="组织已识别主要能源使用。",
            confidence=0.91,
            extractor_version="audit-evidence-v1",
        )
        db.add(ledger)
        db.commit()
        assert ledger.report_section_ids_json == []

        duplicate = AuditEvidenceLedger.model_validate(
            {**ledger.model_dump(), "id": "evidence-2"}
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        coverage = AuditElementCoverage(
            tenant_id="tenant_demo",
            audit_case_id="case-1",
            audit_element_id="4.4.3",
        )
        db.add(coverage)
        db.commit()
        assert coverage.status == "pending"
        assert db.exec(
            select(AuditElementCoverage).where(AuditElementCoverage.audit_case_id == "case-1")
        ).one().material_evidence_count == 0


def test_required_elements_are_stable_and_unique() -> None:
    elements = load_required_elements(["GB/T 23331-2020"])

    assert elements
    assert len({item.id for item in elements}) == len(elements)
    assert all(item.query_templates for item in elements)
    assert all(item.report_section_id for item in elements)


class _FakeEvidenceClient:
    def __init__(self) -> None:
        self.chunk_ids: list[str] = []

    def generate_json(self, _prompt: str, payload: dict[str, object]) -> dict[str, object]:
        chunk_id = str(payload["chunk_id"])
        self.chunk_ids.append(chunk_id)
        return {
            "chunk_id": chunk_id,
            "items": [
                {
                    "audit_element_id": "4.4.3",
                    "evidence_type": "conformity",
                    "evidence_text": f"已核查材料块 {chunk_id}。",
                    "confidence": 0.8,
                }
            ],
        }


def _processor_with_chunks(
    statuses: list[str],
) -> tuple[AuditEvidenceProcessor, AuditCase, list[AuditCaseMaterialChunk], _FakeEvidenceClient, Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    case = AuditCase(
        id="case-1",
        tenant_id="tenant_demo",
        owner_user_id="user-1",
        organization_name="示例组织",
        report_type="再认证审核报告",
        management_systems_json=["GB/T 23331-2020"],
    )
    chunks = [
        AuditCaseMaterialChunk(
            id=f"chunk-{index + 1}",
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            material_id="material-1",
            chunk_index=index,
            start_char=index * 10,
            end_char=(index + 1) * 10,
            content_sha256=f"sha-{index + 1}",
            content=f"审核材料块 {index + 1}",
            processing_status=status,
        )
        for index, status in enumerate(statuses)
    ]
    db.add(case)
    db.add_all(chunks)
    db.commit()
    fake_client = _FakeEvidenceClient()
    processor = AuditEvidenceProcessor(
        db,
        client_factory=lambda _model_config: fake_client,
    )
    return processor, case, chunks, fake_client, db


def test_processor_retries_only_pending_or_failed_chunks() -> None:
    processor, case, chunks, fake_client, db = _processor_with_chunks(
        ["succeeded", "pending", "failed"]
    )
    try:
        summary = processor.process_pending_chunks(case, None, batch_size=2)

        assert fake_client.chunk_ids == [chunks[1].id, chunks[2].id]
        assert summary.total == 3
        assert summary.succeeded == 2
        assert summary.skipped == 1
        assert summary.failed == 0
    finally:
        db.close()


def test_extracted_evidence_must_reference_known_element() -> None:
    with pytest.raises(AuditEvidenceError, match="INVALID_AUDIT_ELEMENT_REFERENCE"):
        validate_extraction_result(
            {
                "chunk_id": "chunk-1",
                "items": [
                    {
                        "audit_element_id": "unknown",
                        "evidence_type": "conformity",
                        "evidence_text": "无来源要素",
                        "confidence": 0.8,
                    }
                ],
            },
            allowed_element_ids={"4.4.3"},
        )
