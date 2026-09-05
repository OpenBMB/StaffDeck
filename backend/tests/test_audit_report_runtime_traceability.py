from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine as create_sqlmodel_engine

from app.db import database
from app.db.models import AuditReportSection, AuditReportVersion


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
