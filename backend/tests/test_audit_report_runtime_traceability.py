from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine as create_sqlmodel_engine, select

from app.audit_cases.elements import load_required_elements
from app.audit_cases.reporting import AuditReportBlocked, AuditReportService
from app.db import database
from app.db.models import (
    AuditCase,
    AuditEvidenceLedger,
    AuditReportSection,
    AuditReportVersion,
    ProjectRuleBinding,
    RuleDefinition,
    RuleSet,
    RuleSetVersion,
)


def _test_session() -> Session:
    engine = create_sqlmodel_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_report_rows_default_to_unconfigured_rule_traceability() -> None:
    with _test_session() as db:
        row = AuditReportVersion(
            tenant_id="tenant_demo",
            audit_case_id="case-1",
            version=1,
        )
        section = AuditReportSection(
            tenant_id="tenant_demo",
            audit_case_id="case-1",
            report_version_id=row.id,
            section_id="summary",
            title="摘要",
            sequence=1,
        )
        db.add(row)
        db.add(section)
        db.commit()
        assert row.rule_set_version_ids_json == []
        assert row.rule_traceability_status == "not_configured"
        assert section.rule_definition_ids_json == []


def test_report_traceability_migration_is_additive_and_idempotent(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-audit-report.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE audit_report_versions (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    audit_case_id VARCHAR NOT NULL,
                    version INTEGER NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'draft',
                    material_version_ids_json JSON NOT NULL DEFAULT '[]',
                    knowledge_base_version_ids_json JSON NOT NULL DEFAULT '[]',
                    coverage_snapshot_json JSON NOT NULL DEFAULT '{}',
                    final_storage_key VARCHAR,
                    lead_auditor_confirmed_by VARCHAR,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE audit_report_sections (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    audit_case_id VARCHAR NOT NULL,
                    report_version_id VARCHAR NOT NULL,
                    section_id VARCHAR NOT NULL,
                    title VARCHAR NOT NULL,
                    sequence INTEGER NOT NULL,
                    audit_element_ids_json JSON NOT NULL DEFAULT '[]',
                    status VARCHAR NOT NULL DEFAULT 'pending',
                    draft_markdown VARCHAR NOT NULL DEFAULT '',
                    citation_ids_json JSON NOT NULL DEFAULT '[]',
                    model_config_id VARCHAR,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error_code VARCHAR,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )

    with engine.begin() as conn:
        database._migrate_audit_report_traceability_schema(
            conn,
            inspect(conn),
            {"audit_report_versions", "audit_report_sections"},
        )
        database._migrate_audit_report_traceability_schema(
            conn,
            inspect(conn),
            {"audit_report_versions", "audit_report_sections"},
        )

    columns = {column["name"] for column in inspect(engine).get_columns("audit_report_versions")}
    assert {"rule_set_version_ids_json", "rule_traceability_status"} <= columns
    section_columns = {column["name"] for column in inspect(engine).get_columns("audit_report_sections")}
    assert "rule_definition_ids_json" in section_columns
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM app_data_migrations "
                "WHERE id = 'audit_report_runtime_traceability_v1'"
            )
        ).scalar_one() == 1


class _RuleAwareReportClient:
    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, object]] = {}

    def generate_text(self, _prompt: str, payload: dict[str, object]) -> str:
        section_id = str(payload["section_id"])
        self.payloads[section_id] = payload
        evidence = payload.get("evidence") or []
        if not evidence:
            return "章节暂无证据 [EVIDENCE:missing]"
        return f"章节依据证据编写 [EVIDENCE:{evidence[0]['id']}]"


def _bound_report_context() -> tuple[Session, AuditCase, RuleSetVersion]:
    db = _test_session()
    case = AuditCase(
        id="case-rules",
        tenant_id="tenant_demo",
        owner_user_id="user-1",
        organization_name="规则追溯组织",
        report_type="再认证审核报告",
        management_systems_json=["GB/T 23331-2020"],
    )
    rule_set = RuleSet(
        id="set-energy",
        tenant_id=case.tenant_id,
        key="energy.audit",
        name="能源审核规则",
        status="published",
    )
    version = RuleSetVersion(
        id="version-energy-1",
        tenant_id=case.tenant_id,
        rule_set_id=rule_set.id,
        version=1,
        status="published",
        content_sha256="f" * 64,
    )
    applicable = RuleDefinition(
        id="rule-report-1",
        tenant_id=case.tenant_id,
        rule_set_version_id=version.id,
        rule_key="report.section.guidance",
        name="报告章节指导",
        workflow_nodes_json=["generate_report_sections"],
        information_domains_json=["audit_report"],
        document_types_json=["audit_report"],
        source_refs_json=[{"ref": "POL-REPORT-1"}],
    )
    excluded = RuleDefinition(
        id="rule-collect-only",
        tenant_id=case.tenant_id,
        rule_set_version_id=version.id,
        rule_key="collect.only",
        name="采集阶段规则",
        workflow_nodes_json=["collect_materials"],
        document_types_json=["audit_plan"],
    )
    binding = ProjectRuleBinding(
        id="binding-energy-1",
        tenant_id=case.tenant_id,
        audit_case_id=case.id,
        rule_set_id=rule_set.id,
        rule_set_version_id=version.id,
        status="current",
        bound_by_user_id="user-1",
    )
    db.add_all([case, rule_set, version, applicable, excluded, binding])
    for index, element in enumerate(load_required_elements(case.management_systems_json)):
        db.add(
            AuditEvidenceLedger(
                id=f"evidence-rules-{index}",
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                audit_element_id=element.id,
                source_kind="case_material",
                source_id="material-1",
                source_version_id="material-1:v1",
                chunk_id=f"chunk-rules-{index}",
                source_ref=f"审核记录.pdf#chunk={index}",
                evidence_type="conformity",
                evidence_text=f"条款 {element.id} 的证据",
                confidence=0.9,
                extractor_version="audit-evidence-v1",
            )
        )
    db.commit()
    return db, case, version


def test_report_version_freezes_current_published_bindings_and_section_rule_ids() -> None:
    db, case, version = _bound_report_context()
    try:
        report = AuditReportService(db).create_version(case)
        assert report.rule_set_version_ids_json == [version.id]
        assert report.rule_traceability_status == "complete"
        sections = db.exec(
            select(AuditReportSection).where(
                AuditReportSection.report_version_id == report.id
            )
        ).all()
        assert sections
        assert all(section.rule_definition_ids_json == ["rule-report-1"] for section in sections)
    finally:
        db.close()


def test_section_generation_receives_only_report_applicable_rule_metadata() -> None:
    db, case, _version = _bound_report_context()
    fake_client = _RuleAwareReportClient()
    try:
        service = AuditReportService(db, client_factory=lambda _config: fake_client)
        report = service.create_version(case)
        service.generate_pending_sections(case, report, None)
        payload = next(iter(fake_client.payloads.values()))
        rules = payload["rules"]
        assert {item["id"] for item in rules} == {"rule-report-1"}
        assert all("evidence_text" not in item for item in rules)
        assert "rule-collect-only" not in json.dumps(payload, ensure_ascii=False)
    finally:
        db.close()


def test_confirmed_publish_is_blocked_without_rule_binding() -> None:
    db = _test_session()
    case = AuditCase(
        id="case-no-rules",
        tenant_id="tenant_demo",
        owner_user_id="user-1",
        organization_name="未配置规则组织",
        report_type="再认证审核报告",
        management_systems_json=["GB/T 23331-2020"],
    )
    db.add(case)
    db.commit()
    try:
        service = AuditReportService(db)
        report = service.create_version(case)
        with pytest.raises(AuditReportBlocked, match="RULE_BINDING_REQUIRED_FOR_PUBLISH"):
            service.publish(case, report, confirmed_by="lead-auditor")
    finally:
        db.close()
