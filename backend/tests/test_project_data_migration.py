from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from app.db.database import run_project_data_backfill
from app.db.models import (
    AuditCase,
    AuditCaseMemberRole,
    ProjectDataFieldDefinition,
    User,
)


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


def test_project_data_backfill_is_safe_when_two_startups_race(tmp_path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent-startup.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE app_data_migrations (id VARCHAR PRIMARY KEY)"))
    with Session(engine) as db:
        db.add_all(
            [
                _user("owner-1", "tenant_demo"),
                _user("member-1", "tenant_demo"),
                AuditCase(
                    id="case-1",
                    tenant_id="tenant_demo",
                    owner_user_id="owner-1",
                    member_user_ids_json=["member-1"],
                    organization_name="甲公司",
                    report_type="认证审核",
                ),
            ]
        )
        db.commit()

    marker_reads = Barrier(2)

    def align_initial_marker_reads(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select id from app_data_migrations"):
            try:
                marker_reads.wait(timeout=1)
            except BrokenBarrierError:
                pass

    event.listen(engine, "before_cursor_execute", align_initial_marker_reads)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_project_data_backfill, engine) for _ in range(2)]
            failures = [future.exception(timeout=10) for future in futures]
    finally:
        event.remove(engine, "before_cursor_execute", align_initial_marker_reads)

    assert failures == [None, None]
    with Session(engine) as db:
        rows = db.exec(
            select(AuditCaseMemberRole).where(AuditCaseMemberRole.audit_case_id == "case-1")
        ).all()
        assert {(row.user_id, row.role) for row in rows} == {
            ("owner-1", "project_admin"),
            ("member-1", "editor"),
        }
        assert len(rows) == 2
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM app_data_migrations "
                "WHERE id = 'project_data_member_roles_v1'"
            )
        ).scalar_one() == 1


def test_project_data_migration_adds_missing_system_field_unique_index(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'missing-system-field-index.db'}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_project_data_system_field_key"))

    run_project_data_backfill(engine)

    with Session(engine) as db:
        db.add(
            ProjectDataFieldDefinition(
                id="field-definition-1",
                tenant_id=None,
                field_key="organization.legal_name",
                label="法定名称",
                value_type="string",
                information_domain="organization",
                scope="organization",
            )
        )
        db.commit()
        db.add(
            ProjectDataFieldDefinition(
                id="field-definition-2",
                tenant_id=None,
                field_key="organization.legal_name",
                label="企业名称",
                value_type="string",
                information_domain="organization",
                scope="organization",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
