from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import knowledge_bases as knowledge_bases_api
from app.db.models import (
    AgentProfile,
    KnowledgeBase,
    KnowledgeBaseAuditEvent,
    KnowledgeBaseVersion,
    Team,
    Tenant,
    User,
)


def _audit_module():
    """加载审计模块本体，供查询与不可变边界测试访问公开类型。"""
    assert importlib.util.find_spec("app.knowledge.audit") is not None, (
        "missing service module: app.knowledge.audit"
    )
    return importlib.import_module("app.knowledge.audit")


def _audit_api():
    """延迟加载审计模块，让缺失实现以可读断言形成 TDD 红灯。"""
    assert importlib.util.find_spec("app.knowledge.errors") is not None, (
        "missing error module: app.knowledge.errors"
    )
    audit_module = _audit_module()
    error_module = importlib.import_module("app.knowledge.errors")
    return (
        audit_module.KnowledgeAuditService,
        error_module.KnowledgeError,
    )


def _session() -> Session:
    """创建独立内存数据库，副作用仅限当前测试。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_query_history(db: Session) -> User:
    """建立跨团队、跨操作者、跨版本和跨租户的审计查询样本。"""
    admin = User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        display_name="知识管理员",
        role="admin",
        password_hash="test",
    )
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(Tenant(id="tenant_other", name="Other"))
    db.add(admin)
    db.add(
        AgentProfile(
            id="agent_publisher",
            tenant_id="tenant_demo",
            name="发布员工",
        )
    )
    db.add(
        Team(
            id="team_a",
            tenant_id="tenant_demo",
            name="内容团队",
            owner_user_id=admin.id,
        )
    )
    db.add(
        Team(
            id="team_b",
            tenant_id="tenant_demo",
            name="增长团队",
            owner_user_id=admin.id,
        )
    )
    db.add(
        KnowledgeBase(
            id="kb_shared",
            tenant_id="tenant_demo",
            name="共享内容知识",
            mode="shared",
        )
    )
    db.add(
        KnowledgeBase(
            id="kb_other",
            tenant_id="tenant_other",
            name="其他租户知识",
            mode="shared",
        )
    )
    db.add(
        KnowledgeBaseVersion(
            id="kbver_1",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            version="1.0.0",
            name="共享内容知识",
        )
    )
    db.add(
        KnowledgeBaseVersion(
            id="kbver_2",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            version="1.1.0",
            name="共享内容知识",
        )
    )
    db.flush()

    service = _audit_module().KnowledgeAuditService(db)
    base_time = datetime(2026, 8, 1, 9, 0, 0, tzinfo=UTC)
    first = service.append_event(
        tenant_id="tenant_demo",
        knowledge_base_id="kb_shared",
        team_id="team_a",
        knowledge_base_version_id="kbver_1",
        actor_type="user",
        actor_id=admin.id,
        action="draft_created",
        reason="建立草稿",
        details={"source_task_id": "task-draft"},
    )
    first.created_at = base_time
    second = service.append_event(
        tenant_id="tenant_demo",
        knowledge_base_id="kb_shared",
        team_id="team_a",
        knowledge_base_version_id="kbver_2",
        actor_type="agent",
        actor_id="agent_publisher",
        action="version_published",
        reason="通过审校后发布",
        details={"source_task_id": "task-publish", "previous_version_id": "kbver_1"},
        idempotency_key="turn-publish-1",  # gitleaks:allow - deterministic test fixture
        request_payload={"draft_version_id": "kbver_2"},
        durable_result={"published_version_id": "kbver_2"},
    )
    second.created_at = base_time + timedelta(minutes=1)
    third = service.append_event(
        tenant_id="tenant_demo",
        knowledge_base_id="kb_shared",
        team_id="team_b",
        knowledge_base_version_id="kbver_1",
        actor_type="user",
        actor_id=admin.id,
        action="version_rolled_back",
        reason="恢复稳定版本",
        details={"previous_version_id": "kbver_2", "target_version_id": "kbver_1"},
    )
    third.created_at = base_time + timedelta(minutes=2)
    cross_tenant = service.append_event(
        tenant_id="tenant_other",
        knowledge_base_id="kb_other",
        team_id=None,
        knowledge_base_version_id=None,
        actor_type="system",
        actor_id="migration",
        action="shared_created",
        reason="其他租户事件",
    )
    cross_tenant.created_at = base_time + timedelta(minutes=3)
    db.commit()
    return admin


def test_human_audit_events_are_append_only_even_for_the_same_action() -> None:
    """人工重复操作产生独立事件，不覆盖历史记录。"""
    service_model, _ = _audit_api()
    with _session() as db:
        service = service_model(db)
        first = service.append_event(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            team_id="team_a",
            knowledge_base_version_id=None,
            actor_type="user",
            actor_id="user_admin",
            action="default_changed",
            reason="设为默认",
            details={"previous": None, "current": "kb_shared"},
        )
        second = service.append_event(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            team_id="team_a",
            knowledge_base_version_id=None,
            actor_type="user",
            actor_id="user_admin",
            action="default_changed",
            reason="再次确认",
            details={"previous": "kb_shared", "current": "kb_shared"},
        )
        db.commit()

        rows = list(
            db.exec(
                select(KnowledgeBaseAuditEvent).order_by(
                    KnowledgeBaseAuditEvent.created_at,
                    KnowledgeBaseAuditEvent.id,
                )
            ).all()
        )

    assert first.id != second.id
    assert [row.reason for row in rows] == ["设为默认", "再次确认"]
    assert rows[0].details_json["current"] == "kb_shared"


def test_agent_idempotency_replays_the_original_durable_result() -> None:
    """Agent 使用相同规范化输入重试时，返回首个事件保存的结果而不新增事件。"""
    service_model, _ = _audit_api()
    with _session() as db:
        service = service_model(db)
        payload = {
            "draft_version_id": "kbver_draft",
            "change_reason": "批准发布",
        }
        assert service.replay_agent_mutation(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            actor_id="agent_publisher",
            action="version_published",
            idempotency_key="turn-1-call-1",
            request_payload=payload,
        ) is None

        event = service.append_event(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            team_id="team_a",
            knowledge_base_version_id="kbver_draft",
            actor_type="agent",
            actor_id="agent_publisher",
            action="version_published",
            idempotency_key="turn-1-call-1",
            reason="批准发布",
            details={"source_task_id": "task_1"},
            request_payload=payload,
            durable_result={
                "previous_version_id": "kbver_1",
                "published_version_id": "kbver_draft",
            },
        )
        db.commit()

        receipt = service.replay_agent_mutation(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            actor_id="agent_publisher",
            action="version_published",
            idempotency_key="turn-1-call-1",
            request_payload={
                "change_reason": "批准发布",
                "draft_version_id": "kbver_draft",
            },
        )
        row_count = len(db.exec(select(KnowledgeBaseAuditEvent)).all())

        assert receipt is not None
        assert receipt.event_id == event.id
        assert receipt.result == {
            "audit_event_id": event.id,
            "previous_version_id": "kbver_1",
            "published_version_id": "kbver_draft",
        }
    assert row_count == 1


def test_agent_idempotency_rejects_changed_input_and_missing_keys() -> None:
    """相同幂等键不能复用于不同输入，Agent 变更也不能省略幂等键。"""
    service_model, error_model = _audit_api()
    with _session() as db:
        service = service_model(db)
        service.append_event(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            team_id="team_a",
            knowledge_base_version_id="kbver_draft",
            actor_type="agent",
            actor_id="agent_publisher",
            action="draft_rejected",
            idempotency_key="turn-2-call-1",
            reason="内容不完整",
            request_payload={"change_reason": "内容不完整"},
            durable_result={"state": "rejected"},
        )
        db.commit()

        with pytest.raises(error_model) as conflict:
            service.replay_agent_mutation(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_shared",
                actor_id="agent_publisher",
                action="draft_rejected",
                idempotency_key="turn-2-call-1",
                request_payload={"change_reason": "来源不可靠"},
            )
        with pytest.raises(error_model) as missing:
            service.append_event(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_shared",
                team_id="team_a",
                knowledge_base_version_id="kbver_other",
                actor_type="agent",
                actor_id="agent_publisher",
                action="draft_rejected",
                reason="来源不可靠",
                request_payload={"change_reason": "来源不可靠"},
                durable_result={"state": "rejected"},
            )

    assert conflict.value.code == "KNOWLEDGE_IDEMPOTENCY_CONFLICT"
    assert conflict.value.status_code == 409
    assert missing.value.code == "KNOWLEDGE_IDEMPOTENCY_REQUIRED"
    assert missing.value.status_code == 400


def test_agent_idempotency_key_cannot_replay_a_different_knowledge_base() -> None:
    """同一 Agent 动作的幂等键只能属于一个知识库，不能跨库串回放结果。"""
    service_model, error_model = _audit_api()
    with _session() as db:
        service = service_model(db)
        payload = {"change_reason": "批准发布"}
        service.append_event(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            team_id="team_a",
            knowledge_base_version_id=None,
            actor_type="agent",
            actor_id="agent_publisher",
            action="version_published",
            idempotency_key="turn-cross-base",
            reason="批准发布",
            request_payload=payload,
            durable_result={"published_version_id": "kbver_shared"},
        )
        db.commit()

        with pytest.raises(error_model) as conflict:
            service.replay_agent_mutation(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_other",
                actor_id="agent_publisher",
                action="version_published",
                idempotency_key="turn-cross-base",
                request_payload=payload,
            )

    assert conflict.value.code == "KNOWLEDGE_IDEMPOTENCY_CONFLICT"
    assert conflict.value.status_code == 409


def test_audit_query_paginates_filters_and_never_crosses_tenants() -> None:
    """查询按时间倒序分页，并组合团队、动作、操作者和版本过滤且不泄漏租户。"""
    with _session() as db:
        _seed_query_history(db)
        service = _audit_module().KnowledgeAuditService(db)

        first_page = service.query_events(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            offset=0,
            limit=2,
        )
        second_page = service.query_events(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            offset=2,
            limit=2,
        )
        filtered = service.query_events(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            offset=0,
            limit=20,
            team_id="team_a",
            action="version_published",
            actor_type="agent",
            actor_id="agent_publisher",
            knowledge_base_version_id="kbver_2",
        )

    assert first_page.total == 3
    assert first_page.has_more is True
    assert [event.action for event in first_page.items] == [
        "version_rolled_back",
        "version_published",
    ]
    assert [event.action for event in second_page.items] == ["draft_created"]
    assert second_page.has_more is False
    assert filtered.total == 1
    assert filtered.items[0].team_name == "内容团队"
    assert filtered.items[0].actor_name == "发布员工"
    assert filtered.items[0].knowledge_base_version == "1.1.0"
    assert "idempotency_receipt" not in filtered.items[0].details


def test_audit_endpoint_returns_filtered_page_and_rejects_cross_tenant_viewers() -> None:
    """HTTP 投影保留分页元数据，且当前用户不能借其他租户参数读取历史。"""
    with _session() as db:
        admin = _seed_query_history(db)
        page = knowledge_bases_api.list_shared_knowledge_audit_events(
            "kb_shared",
            tenant_id="tenant_demo",
            offset=0,
            limit=1,
            team_id="team_a",
            action="version_published",
            actor_type="agent",
            actor_id="agent_publisher",
            version_id="kbver_2",
            db=db,
            current_user=admin,
        )

        with pytest.raises(HTTPException) as exc_info:
            knowledge_bases_api.list_shared_knowledge_audit_events(
                "kb_other",
                tenant_id="tenant_other",
                offset=0,
                limit=20,
                team_id=None,
                action=None,
                actor_type=None,
                actor_id=None,
                version_id=None,
                db=db,
                current_user=admin,
            )

    assert page.total == 1
    assert page.items[0].action == "version_published"
    assert page.items[0].reason == "通过审校后发布"
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "KNOWLEDGE_CONTEXT_MISMATCH"


def test_persisted_audit_events_reject_update_and_delete() -> None:
    """已提交事件在 ORM 写入边界拒绝修改和删除，保证历史只追加。"""
    module = _audit_module()
    with _session() as db:
        event = module.KnowledgeAuditService(db).append_event(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            team_id=None,
            knowledge_base_version_id=None,
            actor_type="user",
            actor_id="user_admin",
            action="shared_created",
            reason="初始创建",
        )
        db.commit()

        event.reason = "篡改后的原因"
        with pytest.raises(module.KnowledgeAuditImmutabilityError):
            db.commit()
        db.rollback()

        persisted = db.get(KnowledgeBaseAuditEvent, event.id)
        assert persisted is not None
        assert persisted.reason == "初始创建"
        db.delete(persisted)
        with pytest.raises(module.KnowledgeAuditImmutabilityError):
            db.commit()
        db.rollback()
        assert db.get(KnowledgeBaseAuditEvent, event.id) is not None
