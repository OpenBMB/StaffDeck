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
