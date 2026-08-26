from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import AuditReportSection, AuditReportVersion


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
