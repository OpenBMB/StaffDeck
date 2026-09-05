from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app.db import database


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
