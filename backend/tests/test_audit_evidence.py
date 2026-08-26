from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit_cases.coverage import calculate_coverage
from app.audit_cases.elements import load_required_elements
from app.audit_cases.evidence import AuditEvidenceProcessor
from app.audit_cases.evidence_schema import AuditEvidenceError, validate_extraction_result
from app.db.models import (
    AuditCase,
    AuditCaseMaterial,
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


def _coverage_case(
    db: Session, intervals: list[tuple[int, int]], statuses: list[str]
) -> AuditCase:
    case = AuditCase(
        id="case-coverage",
        tenant_id="tenant_demo",
        owner_user_id="user-1",
        organization_name="示例组织",
        report_type="再认证审核报告",
        management_systems_json=["GB/T 23331-2020"],
    )
    material = AuditCaseMaterial(
        id="material-coverage",
        tenant_id=case.tenant_id,
        audit_case_id=case.id,
        attachment_id="attachment-1",
        material_type="audit_record",
        filename="审核记录.txt",
        content_type="text/plain",
        sha256="material-sha",
        size=1800,
        storage_key="case-coverage/material-coverage/source",
        characters=1800,
        extraction_status="succeeded",
        processing_status="succeeded",
    )
    chunks = [
        AuditCaseMaterialChunk(
            id=f"coverage-chunk-{index + 1}",
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            material_id=material.id,
            chunk_index=index,
            start_char=start,
            end_char=end,
            content_sha256=f"coverage-sha-{index + 1}",
            content="x" * (end - start),
            processing_status=status,
        )
        for index, ((start, end), status) in enumerate(zip(intervals, statuses, strict=True))
    ]
    coverages = [
        AuditElementCoverage(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            audit_element_id=element.id,
            status="evidence_found",
        )
        for element in load_required_elements(case.management_systems_json)
    ]
    db.add(case)
    db.add(material)
    db.add_all(chunks)
    db.add_all(coverages)
    db.commit()
    return case


def test_coverage_gate_requires_all_files_chunks_and_elements() -> None:
    with _test_session() as db:
        case = _coverage_case(db, [(0, 900), (900, 1800)], ["succeeded", "pending"])

        snapshot = calculate_coverage(db, case)

        assert snapshot.file_coverage == 1.0
        assert snapshot.chunk_coverage == 0.5
        assert snapshot.element_coverage == 1.0
        assert snapshot.publish_allowed is False
        assert snapshot.blockers == ["CHUNK_COVERAGE_INCOMPLETE"]


def test_chunk_interval_gap_blocks_publish_even_when_status_succeeded() -> None:
    with _test_session() as db:
        case = _coverage_case(db, [(0, 900), (901, 1800)], ["succeeded", "succeeded"])

        snapshot = calculate_coverage(db, case)

        assert "CHUNK_INTERVAL_GAP" in snapshot.blockers
        assert snapshot.publish_allowed is False
