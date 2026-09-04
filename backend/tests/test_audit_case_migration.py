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
