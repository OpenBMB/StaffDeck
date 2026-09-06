from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit_cases.reporting import AuditReportService
from app.db.models import AuditCase, AuditEvidenceLedger, AuditReportSection, AuditReportVersion


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_report_sections_have_stable_identity_and_draft_defaults() -> None:
    with _test_session() as db:
        report = AuditReportVersion(
            tenant_id="tenant_demo",
            audit_case_id="case-1",
            version=1,
        )
        db.add(report)
        db.commit()

        section = AuditReportSection(
            tenant_id="tenant_demo",
            audit_case_id="case-1",
            report_version_id=report.id,
            section_id="management_summary",
            title="管理体系概况",
            sequence=1,
        )
        db.add(section)
        db.commit()
        assert report.status == "draft"
        assert section.status == "pending"
        assert section.draft_markdown == ""
        assert section.citation_ids_json == []

        duplicate = AuditReportSection(
            tenant_id="tenant_demo",
            audit_case_id="case-1",
            report_version_id=report.id,
            section_id="management_summary",
            title="重复章节",
            sequence=1,
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()


class _FakeReportClient:
    def __init__(self) -> None:
        self.generated_section_ids: list[str] = []
        self.payloads: dict[str, dict[str, object]] = {}

    def generate_text(self, _prompt: str, payload: dict[str, object]) -> str:
        section_id = str(payload["section_id"])
        self.generated_section_ids.append(section_id)
        self.payloads[section_id] = payload
        evidence = payload["evidence"]
        evidence_id = str(evidence[0]["id"])
        return f"本章节依据台账证据编写。[EVIDENCE:{evidence_id}]"

    def payload_for(self, section_id: str) -> dict[str, object]:
        return self.payloads[section_id]


def _report_service(
    section_statuses: list[str] | None = None,
) -> tuple[AuditReportService, _FakeReportClient, AuditReportVersion, AuditCase, Session]:
    db = _test_session()
    case = AuditCase(
        id="case-report",
        tenant_id="tenant_demo",
        owner_user_id="user-1",
        organization_name="示例组织",
        report_type="再认证审核报告",
        management_systems_json=["GB/T 23331-2020"],
    )
    report = AuditReportVersion(
        id="report-1",
        tenant_id=case.tenant_id,
        audit_case_id=case.id,
        version=1,
    )
    statuses = section_statuses or ["pending", "pending", "pending"]
    sections = [
        AuditReportSection(
            id=f"section-{index + 1}",
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            report_version_id=report.id,
            section_id="management_summary" if index == 0 else f"section-{index + 1}",
            title="管理体系概况" if index == 0 else f"章节 {index + 1}",
            sequence=index + 1,
            audit_element_ids_json=["4.4.3", "4.4.4"] if index == 0 else ["4.6.1"],
            status=status,
        )
        for index, status in enumerate(statuses)
    ]
    evidence_rows = [
        AuditEvidenceLedger(
            id=f"evidence-{index + 1}",
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            audit_element_id=element_id,
            source_kind="case_material",
            source_id="material-1",
            source_version_id="material-1:v1",
            chunk_id=f"chunk-{index + 1}",
            source_ref=f"审核记录.pdf#chunk={index}",
            evidence_type="conformity",
            evidence_text="unrelated-secret" if element_id == "4.6.1" else f"条款 {element_id} 的证据",
            confidence=0.9,
            extractor_version="audit-evidence-v1",
        )
        for index, element_id in enumerate(["4.4.3", "4.4.4", "4.6.1"])
    ]
    db.add(case)
    db.add(report)
    db.add_all(sections)
    db.add_all(evidence_rows)
    db.commit()
    fake_client = _FakeReportClient()
    service = AuditReportService(db, client_factory=lambda _model_config: fake_client)
    return service, fake_client, report, case, db


def test_section_generation_receives_only_mapped_evidence() -> None:
    service, fake_client, report, case, db = _report_service()
    try:
        service.generate_pending_sections(case, report, None)

        management_payload = fake_client.payload_for("management_summary")
        assert {item["audit_element_id"] for item in management_payload["evidence"]} == {
            "4.4.3",
            "4.4.4",
        }
        assert "unrelated-secret" not in json.dumps(management_payload, ensure_ascii=False)
    finally:
        db.close()


def test_report_resume_skips_completed_sections() -> None:
    service, fake_client, report, case, db = _report_service(
        section_statuses=["succeeded", "failed", "pending"]
    )
    try:
        service.generate_pending_sections(case, report, None)

        assert fake_client.generated_section_ids == ["section-2", "section-3"]
        sections = db.exec(
            select(AuditReportSection).where(AuditReportSection.report_version_id == report.id)
        ).all()
        assert next(section for section in sections if section.id == "section-1").status == "succeeded"
    finally:
        db.close()
