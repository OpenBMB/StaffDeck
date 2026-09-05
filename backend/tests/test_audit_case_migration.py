from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

from app.db import database
from app.db.models import AuditCase, User


def test_audit_case_session_migration_is_idempotent(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-sessions.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE sessions (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    user_id VARCHAR,
                    agent_id VARCHAR
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO sessions (id, tenant_id, user_id) VALUES "
                "('session-legacy', 'tenant_a', 'user_a')"
            )
        )

        database._migrate_audit_case_schema(conn, inspect(engine), {"sessions"})
        database._migrate_audit_case_schema(conn, inspect(engine), {"sessions"})

        columns = {column["name"] for column in inspect(conn).get_columns("sessions")}
        assert "audit_case_id" in columns
        assert conn.execute(
            text("SELECT audit_case_id FROM sessions WHERE id = 'session-legacy'")
        ).scalar_one() is None
        assert any(
            index["name"] == "ix_sessions_audit_case_id"
            for index in inspect(conn).get_indexes("sessions")
        )


def test_audit_case_agent_binding_migration_preserves_legacy_projects(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-audit-cases.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE audit_cases (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    owner_user_id VARCHAR NOT NULL,
                    organization_name VARCHAR NOT NULL,
                    report_type VARCHAR NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO audit_cases "
                "(id, tenant_id, owner_user_id, organization_name, report_type) VALUES "
                "('case-legacy', 'tenant_a', 'user_a', '存量企业', '监督审核')"
            )
        )

        database._migrate_audit_case_schema(conn, inspect(engine), {"audit_cases"})
        database._migrate_audit_case_schema(conn, inspect(engine), {"audit_cases"})

        columns = {column["name"] for column in inspect(conn).get_columns("audit_cases")}
        assert {"agent_id", "knowledge_scope_mode"} <= columns
        row = conn.execute(
            text(
                "SELECT agent_id, knowledge_scope_mode FROM audit_cases "
                "WHERE id = 'case-legacy'"
            )
        ).one()
        assert row.agent_id is None
        assert row.knowledge_scope_mode == "custom"
        assert any(
            index["name"] == "ix_audit_cases_agent_id"
            for index in inspect(conn).get_indexes("audit_cases")
        )

def test_project_data_backfill_preserves_audit_case_columns_and_legacy_fields(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-audit-case.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            User(
                id="owner-1",
                tenant_id="tenant_demo",
                username="owner-1",
                password_hash="not-used-in-migration-test",
            )
        )
        db.add(
            AuditCase(
                id="case-1",
                tenant_id="tenant_demo",
                owner_user_id="owner-1",
                member_user_ids_json=["legacy-member-1"],
                organization_name="甲公司",
                report_type="认证审核",
                management_systems_json=["EnMS"],
            )
        )
        db.commit()

    before_columns = {
        column["name"] for column in inspect(engine).get_columns("audit_cases")
    }
    database.run_project_data_backfill(engine)
    after_columns = {
        column["name"] for column in inspect(engine).get_columns("audit_cases")
    }

    with Session(engine) as db:
        case = db.exec(select(AuditCase).where(AuditCase.id == "case-1")).one()
        assert after_columns == before_columns
        assert case.owner_user_id == "owner-1"
        assert case.member_user_ids_json == ["legacy-member-1"]
        assert case.organization_name == "甲公司"
        assert case.report_type == "认证审核"
        assert case.management_systems_json == ["EnMS"]
