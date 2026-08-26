from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import AuditElementCoverage, AuditEvidenceLedger


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
