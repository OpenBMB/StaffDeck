from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit_cases.schema import (
    AuditCaseAccessDenied,
    AuditCaseManagementPage,
    AuditCaseMemberUpdate,
    AuditCaseUpdate,
)
from app.audit_cases.service import AuditCaseService
from app.db.models import (
    AuditCase,
    AuditCaseEvent,
    AuditCaseMaterial,
    AuditCaseMemberRole,
    Tenant,
    User,
)


@pytest.fixture
def management_context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit-case-management.db'}")
    SQLModel.metadata.create_all(engine)
    admin = User(
        id="user-admin",
        tenant_id="tenant-demo",
        username="admin",
        display_name="管理员",
        role="admin",
        source="web",
        password_hash="test",
    )
    member = User(
        id="user-member",
        tenant_id="tenant-demo",
        username="member",
        display_name="项目成员",
        role="member",
        source="web",
        password_hash="test",
    )
    channel_user = User(
        id="user-channel",
        tenant_id="tenant-demo",
        username="channel-user",
        role="member",
        source="wechat",
        password_hash="test",
    )
    other_tenant_user = User(
        id="user-other",
        tenant_id="tenant-other",
        username="other",
        role="member",
        source="web",
        password_hash="test",
    )
    case = AuditCase(
        id="case-management",
        tenant_id="tenant-demo",
        owner_user_id=admin.id,
        organization_name="原始企业",
        report_type="再认证",
        management_systems_json=["能源管理体系"],
    )
    with Session(engine, expire_on_commit=False) as db:
        db.add(Tenant(id="tenant-demo", name="Demo"))
        db.add(Tenant(id="tenant-other", name="Other"))
        db.add_all([admin, member, channel_user, other_tenant_user, case])
        db.commit()
        db.refresh(case)
        yield db, admin, member, channel_user, other_tenant_user, case


def test_update_case_changes_only_editable_fields_and_records_event(management_context) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context
    original_owner = case.owner_user_id
    updated = AuditCaseService(db).update_case(
        case,
        admin,
        AuditCaseUpdate(
            organization_name="更新后的企业",
            report_type="监督",
            management_systems=["能源管理体系", "质量管理体系"],
            knowledge_base_version_ids=[],
        ),
    )

    assert updated.organization_name == "更新后的企业"
    assert updated.report_type == "监督"
    assert updated.management_systems_json == ["能源管理体系", "质量管理体系"]
    assert updated.owner_user_id == original_owner
    events = db.exec(
        select(AuditCaseEvent).where(AuditCaseEvent.audit_case_id == case.id)
    ).all()
    assert events[-1].event_type == "audit_case.updated"


def test_replace_members_rejects_foreign_or_channel_users_as_one_transaction(
    management_context,
) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context
    with pytest.raises(AuditCaseAccessDenied, match="internal project member"):
        AuditCaseService(db).replace_members(
            case,
            admin,
            AuditCaseMemberUpdate(member_user_ids=["user-member", "user-channel", "user-other"]),
        )

    db.refresh(case)
    assert case.member_user_ids_json == []
    assert db.exec(
        select(AuditCaseEvent).where(
            AuditCaseEvent.audit_case_id == case.id,
            AuditCaseEvent.event_type == "audit_case.members_replaced",
        )
    ).all() == []


def test_case_access_supports_explicit_roles_and_legacy_members(management_context) -> None:
    db, admin, member, _channel_user, other_tenant_user, case = management_context
    reviewer = User(
        id="user-reviewer",
        tenant_id=case.tenant_id,
        username="reviewer",
        display_name="复核人",
        role="member",
        source="web",
        password_hash="test",
    )
    case.member_user_ids_json = [member.id]
    db.add_all(
        [
            case,
            reviewer,
            AuditCaseMemberRole(
                id="role-reviewer",
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                user_id=reviewer.id,
                role="reviewer",
                created_by_user_id=admin.id,
            ),
        ]
    )
    db.commit()
    service = AuditCaseService(db)

    assert service.can_access(case, admin) is True
    assert service.can_access(case, member) is True
    assert service.can_access(case, reviewer) is True
    assert service.can_access(case, other_tenant_user) is False


def test_management_list_aggregates_only_current_material_counts(management_context) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context
    now = datetime.now(UTC)
    db.add_all(
        [
            AuditCaseMaterial(
                id="material-ready",
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                attachment_id="attachment-ready",
                material_type="audit_record",
                filename="记录.txt",
                content_type="text/plain",
                sha256="sha-ready",
                size=10,
                storage_key="audit_cases/case-management/material-ready/raw",
                characters=10,
                extraction_status="succeeded",
                processing_status="succeeded",
                version=1,
                is_current=True,
                created_at=now,
                updated_at=now,
            ),
            AuditCaseMaterial(
                id="material-failed",
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                attachment_id="attachment-failed",
                material_type="audit_plan",
                filename="计划.txt",
                content_type="text/plain",
                sha256="sha-failed",
                size=10,
                storage_key="audit_cases/case-management/material-failed/raw",
                characters=0,
                extraction_status="failed",
                processing_status="failed",
                version=1,
                is_current=True,
                created_at=now,
                updated_at=now,
            ),
            AuditCaseMaterial(
                id="material-history",
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                attachment_id="attachment-history",
                material_type="audit_record",
                filename="旧记录.txt",
                content_type="text/plain",
                sha256="sha-history",
                size=10,
                storage_key="audit_cases/case-management/material-history/raw",
                characters=10,
                extraction_status="succeeded",
                processing_status="succeeded",
                version=1,
                is_current=False,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    db.commit()

    page = AuditCaseService(db).list_management_cases(
        admin,
        tenant_id=case.tenant_id,
        limit=20,
        offset=0,
    )

    assert isinstance(page, AuditCaseManagementPage)
    item = next(row for row in page.items if row.id == case.id)
    assert item.material_total == 2
    assert item.material_ready == 1
    assert item.material_failed == 1
