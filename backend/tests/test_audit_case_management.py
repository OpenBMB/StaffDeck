from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit_cases.schema import (
    AuditCaseAccessDenied,
    AuditCaseCreate,
    AuditCaseManagementPage,
    AuditCaseMemberUpdate,
    AuditCaseUpdate,
)
from app.audit_cases.service import AuditCaseService
from app.db.models import (
    AgentKnowledgeBranch,
    AgentProfile,
    AgentResourceBinding,
    AuditCase,
    AuditCaseEvent,
    AuditCaseMaterial,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
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
    case.knowledge_scope_mode = "agent_default"
    db.add(case)
    db.commit()
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
    assert updated.knowledge_scope_mode == "custom"
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


def test_management_list_aggregates_only_current_material_counts(management_context) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context
    now = datetime.now(timezone.utc)
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


def test_management_options_recommend_populated_version_and_flag_name_collisions(
    management_context,
) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context
    primary_base = KnowledgeBase(
        id="kb-primary", tenant_id=case.tenant_id, name="GBT 23331-2020"
    )
    imported_base = KnowledgeBase(
        id="kb-imported", tenant_id=case.tenant_id, name="GBT_23331_2020"
    )
    empty_trunk = KnowledgeBaseVersion(
        id="kbver-primary-trunk",
        tenant_id=case.tenant_id,
        knowledge_base_id=primary_base.id,
        name=primary_base.name,
        version="1.0.0",
    )
    populated_branch = KnowledgeBaseVersion(
        id="kbver-primary-branch",
        tenant_id=case.tenant_id,
        knowledge_base_id=primary_base.id,
        name=primary_base.name,
        version="1.0.0-branch.agent_auditor.1",
    )
    imported_version = KnowledgeBaseVersion(
        id="kbver-imported",
        tenant_id=case.tenant_id,
        knowledge_base_id=imported_base.id,
        name=imported_base.name,
        version="1.0.0",
    )
    document = KnowledgeDocument(
        id="document-primary",
        tenant_id=case.tenant_id,
        knowledge_base_id=primary_base.id,
        knowledge_base_version_id=populated_branch.id,
        filename="GBT 23331-2020.md",
        file_type="md",
        status="ready",
    )
    bucket = KnowledgeBucket(
        id="bucket-primary",
        tenant_id=case.tenant_id,
        knowledge_base_id=primary_base.id,
        knowledge_base_version_id=populated_branch.id,
        document_id=document.id,
        bucket_key="standard",
        title="标准正文",
        summary="能源管理体系标准",
    )
    chunk = KnowledgeChunk(
        id="chunk-primary",
        tenant_id=case.tenant_id,
        knowledge_base_id=primary_base.id,
        knowledge_base_version_id=populated_branch.id,
        document_id=document.id,
        bucket_id=bucket.id,
        chunk_index=0,
        content="能源管理体系要求",
    )
    db.add_all(
        [
            primary_base,
            imported_base,
            empty_trunk,
            populated_branch,
            imported_version,
            document,
            bucket,
            chunk,
        ]
    )
    db.commit()

    options = AuditCaseService(db).list_management_options(admin, tenant_id=case.tenant_id)

    assert options.audit_types[0].value == "一阶段审核"
    assert options.audit_types[0].code == "A"
    assert any(item.value == "能源管理体系" for item in options.management_systems)
    assert any(item.value == "audit_notice" for item in options.material_types)
    versions = {item.id: item for item in options.knowledge_versions}
    assert versions[empty_trunk.id].document_count == 0
    assert versions[empty_trunk.id].recommended is False
    assert versions[populated_branch.id].document_count == 1
    assert versions[populated_branch.id].chunk_count == 1
    assert versions[populated_branch.id].is_agent_branch is True
    assert versions[populated_branch.id].recommended is True
    assert versions[imported_version.id].duplicate_group == "gbt233312020"
    assert versions[populated_branch.id].duplicate_group == "gbt233312020"


def _add_agent_knowledge_scope(db: Session, tenant_id: str):
    agent = AgentProfile(
        id="agent-energy-auditor",
        tenant_id=tenant_id,
        name="能源管理审核员",
        status="active",
    )
    knowledge_base = KnowledgeBase(
        id="kb-energy-audit",
        tenant_id=tenant_id,
        name="能源管理审核知识库",
        metadata_json={
            "scope": "agent_private",
            "visibility": "agent_private",
            "owner_agent_id": agent.id,
        },
    )
    version = KnowledgeBaseVersion(
        id="kbver-energy-agent",
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base.id,
        name=knowledge_base.name,
        version="1.0.0-branch.agent-energy-auditor.1",
    )
    branch = AgentKnowledgeBranch(
        id="agentkb-energy-auditor",
        tenant_id=tenant_id,
        agent_id=agent.id,
        knowledge_base_id=knowledge_base.id,
        base_version="1.0.0",
        head_version=version.version,
    )
    binding = AgentResourceBinding(
        id="agentres-energy-auditor",
        tenant_id=tenant_id,
        agent_id=agent.id,
        resource_type="knowledge_base",
        resource_id=knowledge_base.id,
        status="active",
        metadata_json={
            "scope": "agent_private",
            "visibility": "agent_private",
            "owner_agent_id": agent.id,
        },
    )
    db.add_all([agent, knowledge_base, version, branch, binding])
    db.commit()
    return agent, version


def test_management_options_expose_each_employee_default_knowledge_snapshot(
    management_context,
) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context
    agent, version = _add_agent_knowledge_scope(db, case.tenant_id)

    options = AuditCaseService(db).list_management_options(admin, tenant_id=case.tenant_id)

    employee = next(item for item in options.agent_options if item.id == agent.id)
    assert employee.name == "能源管理审核员"
    assert employee.knowledge_base_version_ids == [version.id]


def test_create_case_freezes_employee_default_knowledge_snapshot(management_context) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context
    agent, version = _add_agent_knowledge_scope(db, case.tenant_id)

    created = AuditCaseService(db).create_case(
        admin,
        AuditCaseCreate(
            tenant_id=case.tenant_id,
            organization_name="继承知识库企业",
            report_type="第一次监督审核",
            agent_id=agent.id,
            knowledge_scope_mode="agent_default",
            knowledge_base_version_ids=[],
        ),
    )

    assert created.agent_id == agent.id
    assert created.knowledge_scope_mode == "agent_default"
    assert created.knowledge_base_version_ids_json == [version.id]


def test_create_case_keeps_admin_custom_knowledge_override(management_context) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context
    agent, version = _add_agent_knowledge_scope(db, case.tenant_id)
    extra_version = KnowledgeBaseVersion(
        id="kbver-custom-audit",
        tenant_id=case.tenant_id,
        knowledge_base_id="kb-custom-audit",
        name="项目补充知识库",
        version="1.0.0",
    )
    db.add(extra_version)
    db.commit()

    created = AuditCaseService(db).create_case(
        admin,
        AuditCaseCreate(
            tenant_id=case.tenant_id,
            organization_name="自定义知识库企业",
            report_type="再认证审核",
            agent_id=agent.id,
            knowledge_scope_mode="custom",
            knowledge_base_version_ids=[extra_version.id],
        ),
    )

    assert created.knowledge_scope_mode == "custom"
    assert created.knowledge_base_version_ids_json == [extra_version.id]
    assert version.id not in created.knowledge_base_version_ids_json


def test_create_case_rejects_missing_agent_for_default_scope(management_context) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context

    with pytest.raises(AuditCaseAccessDenied, match="agent is required"):
        AuditCaseService(db).create_case(
            admin,
            AuditCaseCreate(
                tenant_id=case.tenant_id,
                organization_name="缺少员工企业",
                report_type="再认证审核",
                knowledge_scope_mode="agent_default",
            ),
        )


def test_create_case_rejects_invalid_custom_knowledge_version(management_context) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context
    agent, _version = _add_agent_knowledge_scope(db, case.tenant_id)

    with pytest.raises(AuditCaseAccessDenied, match="invalid knowledge base version"):
        AuditCaseService(db).create_case(
            admin,
            AuditCaseCreate(
                tenant_id=case.tenant_id,
                organization_name="无效知识版本企业",
                report_type="再认证审核",
                agent_id=agent.id,
                knowledge_scope_mode="custom",
                knowledge_base_version_ids=["kbver-missing"],
            ),
        )


def test_management_options_include_versions_materialized_for_employee_scope(
    management_context,
) -> None:
    db, admin, _member, _channel_user, _other_tenant_user, case = management_context
    tenant_id = case.tenant_id
    agent = AgentProfile(
        id="agent-materialized-auditor",
        tenant_id=tenant_id,
        name="待实例化知识审核员",
        status="active",
    )
    knowledge_base = KnowledgeBase(
        id="kb-materialized-audit",
        tenant_id=tenant_id,
        name="待实例化知识库",
        status="active",
    )
    branch = AgentKnowledgeBranch(
        id="agentkb-materialized-audit",
        tenant_id=tenant_id,
        agent_id=agent.id,
        knowledge_base_id=knowledge_base.id,
        base_version="1.0.0",
        head_version="1.0.0-branch.agent-materialized-auditor.1",
        status="active",
    )
    binding = AgentResourceBinding(
        id="agentres-materialized-audit",
        tenant_id=tenant_id,
        agent_id=agent.id,
        resource_type="knowledge_base",
        resource_id=knowledge_base.id,
        status="active",
        metadata_json={"scope": "agent_private"},
    )
    db.add_all([agent, knowledge_base, branch, binding])
    db.commit()

    options = AuditCaseService(db).list_management_options(admin, tenant_id=tenant_id)

    employee = next(item for item in options.agent_options if item.id == agent.id)
    assert len(employee.knowledge_base_version_ids) == 1
    assert employee.knowledge_base_version_ids[0] in {
        version.id for version in options.knowledge_versions
    }
