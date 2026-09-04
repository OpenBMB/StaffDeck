from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel, select

from app.db.database import run_project_data_backfill
from app.db.models import AuditCase, AuditCaseMemberRole, User


def _user(user_id: str, tenant_id: str) -> User:
    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=user_id,
        password_hash="not-used-in-migration-test",
    )


def test_project_data_migration_backfills_owner_and_legacy_members(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE app_data_migrations (id VARCHAR PRIMARY KEY)"))

    with Session(engine) as db:
        db.add_all(
            [
                _user("owner-1", "tenant_demo"),
                _user("member-1", "tenant_demo"),
                _user("reviewer-1", "tenant_demo"),
                AuditCase(
                    id="case-1",
                    tenant_id="tenant_demo",
                    owner_user_id="owner-1",
                    member_user_ids_json=["member-1", "reviewer-1", "member-1"],
                    organization_name="甲公司",
                    report_type="认证审核",
                ),
                AuditCaseMemberRole(
                    id="role-reviewer",
                    tenant_id="tenant_demo",
                    audit_case_id="case-1",
                    user_id="reviewer-1",
                    role="reviewer",
                    created_by_user_id="owner-1",
                ),
            ]
        )
        db.commit()

    run_project_data_backfill(engine)
    run_project_data_backfill(engine)

    with Session(engine) as db:
        rows = db.exec(
            select(AuditCaseMemberRole).where(AuditCaseMemberRole.audit_case_id == "case-1")
        ).all()
        assert {(row.user_id, row.role) for row in rows} == {
            ("owner-1", "project_admin"),
            ("member-1", "editor"),
            ("reviewer-1", "reviewer"),
        }
        assert len(rows) == 3

    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM app_data_migrations "
                "WHERE id = 'project_data_member_roles_v1'"
            )
        ).scalar_one() == 1


def test_project_data_migration_skips_invalid_or_cross_tenant_users(
    tmp_path,
    caplog,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'invalid-legacy-members.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                _user("owner-1", "tenant_demo"),
                _user("foreign-1", "tenant_other"),
                AuditCase(
                    id="case-1",
                    tenant_id="tenant_demo",
                    owner_user_id="owner-1",
                    member_user_ids_json=["foreign-1", "missing-1", "", None, 123],
                    organization_name="甲公司",
                    report_type="认证审核",
                ),
            ]
        )
        db.commit()

    with caplog.at_level(logging.WARNING, logger="app.db.database"):
        run_project_data_backfill(engine)

    with Session(engine) as db:
        rows = db.exec(
            select(AuditCaseMemberRole).where(AuditCaseMemberRole.audit_case_id == "case-1")
        ).all()
        assert {(row.user_id, row.role) for row in rows} == {
            ("owner-1", "project_admin")
        }
    assert "foreign-1" in caplog.text
    assert "missing-1" in caplog.text
    assert "malformed" in caplog.text
