from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api import agents as agents_api
from app.api.agents import get_agent_work_record
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    GeneralSkill,
    KnowledgeBase,
    Message,
    ScheduledTask,
    Skill,
    Tenant,
    Tool,
    User,
)


def test_work_record_returns_timezone_aware_reply_and_activity_times(monkeypatch) -> None:
    with _test_session() as db:
        owner, other = _seed_users(db)
        agent = AgentProfile(
            id="agent_work_record",
            tenant_id="tenant_demo",
            name="工作记录员工",
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        skill = Skill(
            id="skill_bound",
            tenant_id="tenant_demo",
            skill_id="travel_v1",
            name="差旅报销",
            description="差旅流程",
            content_json={},
            status="published",
        )
        general_skill = GeneralSkill(
            id="general_bound",
            tenant_id="tenant_demo",
            slug="weather",
            name="天气查询",
            skill_markdown="# weather",
            status="published",
        )
        knowledge = KnowledgeBase(
            id="kb_bound",
            tenant_id="tenant_demo",
            name="差旅制度",
            status="active",
        )
        tool = Tool(
            id="tool_bound",
            tenant_id="tenant_demo",
            name="expense.query",
            display_name="额度查询",
            method="POST",
            url="http://example.test/query",
            enabled=True,
        )
        db.add(agent)
        db.add(skill)
        db.add(general_skill)
        db.add(knowledge)
        db.add(tool)
        for index, (resource_type, resource_id) in enumerate(
            [
                ("skill", skill.id),
                ("general_skill", general_skill.id),
                ("knowledge_base", knowledge.id),
                ("tool", tool.id),
            ]
        ):
            db.add(
                AgentResourceBinding(
                    id=f"binding_{index}",
                    tenant_id="tenant_demo",
                    agent_id=agent.id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    created_at=datetime(2026, 7, 13, 8 + index, 0, 0),
                )
            )

        db.add(
            ChatSession(
                id="session_owner",
                tenant_id="tenant_demo",
                user_id=owner.id,
                agent_id=agent.id,
            )
        )
        db.add(
            ChatSession(
                id="session_other",
                tenant_id="tenant_demo",
                user_id=other.id,
                agent_id=agent.id,
            )
        )
        db.add(
            Message(
                id="reply_before_midnight",
                tenant_id="tenant_demo",
                session_id="session_owner",
                role="assistant",
                content="上一天",
                created_at=datetime(2026, 7, 14, 15, 30, 0),
            )
        )
        db.add(
            Message(
                id="reply_after_midnight",
                tenant_id="tenant_demo",
                session_id="session_owner",
                role="assistant",
                content="当天",
                created_at=datetime(2026, 7, 14, 16, 30, 0),
            )
        )
        db.add(
            Message(
                id="owner_user_message",
                tenant_id="tenant_demo",
                session_id="session_owner",
                role="user",
                content="不计入回复",
                created_at=datetime(2026, 7, 14, 16, 31, 0),
            )
        )
        db.add(
            Message(
                id="other_user_reply",
                tenant_id="tenant_demo",
                session_id="session_other",
                role="assistant",
                content="不可见",
                created_at=datetime(2026, 7, 14, 16, 32, 0),
            )
        )
        db.add(
            ScheduledTask(
                id="task_owner",
                tenant_id="tenant_demo",
                agent_id=agent.id,
                created_by_user_id=owner.id,
                title="每日汇报",
                prompt="生成汇报",
                last_run_at=datetime(2026, 7, 14, 1, 0, 0),
                next_run_at=datetime(2026, 7, 15, 1, 0, 0),
            )
        )
        db.commit()
        monkeypatch.setattr(agents_api, "utc_now", lambda: datetime(2026, 7, 15, 1, 0, 0))

        result = get_agent_work_record(
            agent.id,
            tenant_id="tenant_demo",
            timezone="Asia/Shanghai",
            db=db,
            current_user=owner,
        )

        assert result.generated_at == "2026-07-15T01:00:00Z"
        assert result.reply_stats.total == 2
        assert result.reply_stats.today == 1
        assert result.reply_stats.by_day == {"2026-07-14": 1, "2026-07-15": 1}
        assert len([event for event in result.events if event.kind == "chat"]) == 2
        assert {event.kind for event in result.events} == {
            "chat",
            "task",
            "sop",
            "tool",
            "knowledge",
            "skill",
        }
        assert all(event.timestamp.endswith("Z") for event in result.events)
        assert {
            (event.phase, event.timestamp)
            for event in result.events
            if event.kind == "task"
        } == {
            ("last_run", "2026-07-14T01:00:00Z"),
            ("next_run", "2026-07-15T01:00:00Z"),
        }


def test_remote_staff_history_does_not_need_local_staff_or_resource_rows(monkeypatch):
    from types import SimpleNamespace as NS
    from staffdeck_harness.modules.registry import ModuleRegistry, manifest
    from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
    from staffdeck_harness.contracts.security import ResourceRef, SecurityContext
    from staffdeck_harness.contracts.staff import StaffProfile, StaffResourceAssignment
    from staffdeck_harness.security.oss_local import build_oss_local_profile
    with _test_session() as db:
        owner, _ = _seed_users(db)
        ref = ResourceRef('agent', 'remote', 'tenant_demo', {'owner_user_id': owner.id})
        source = NS(reference=lambda ctx: ref, profile=lambda ctx: StaffProfile('remote', 'tenant_demo', 'Remote', 'active', ref),
            resolve=lambda *a: pytest.fail('No model/execution snapshot for history'), model=lambda *a: pytest.fail('No model resolution'),
            resource_timeline=lambda ctx, **kw: [StaffResourceAssignment('assigned', 'tenant_demo', 'remote', 'knowledge', datetime(2026, 9, 10), 'Remote knowledge')])
        registry = ModuleRegistry()
        for slot, provider in ((SlotName.STAFF_SOURCE, source), (SlotName.IDENTITY_SOURCE,
                NS(resolve=lambda ctx, adapter: SecurityContext(owner.id, 'tenant_demo')))):
            registry.install(manifest(slot.value, slot.value, kind=ModuleKind.TRUSTED, slots=[slot]),
                NS(build=lambda db, provider=provider: provider), slot=slot)
        registry.seal()
        registry.security_profile = build_oss_local_profile()
        db.info['staffdeck_registry'] = registry
        monkeypatch.setattr(agents_api, '_get_agent', lambda *a: pytest.fail('No local staff lookup'))
        monkeypatch.setattr(agents_api, '_local_agent_resource_timeline_events', lambda *a: pytest.fail('No local resource lookup'))
        result = get_agent_work_record('remote', tenant_id='tenant_demo', timezone='Asia/Shanghai', db=db, current_user=owner)
        assert result.reply_stats.total == 0
        assert [(e.kind, e.label) for e in result.events] == [('knowledge', 'Remote knowledge')]


def test_work_record_rejects_invalid_timezone_and_private_agent_access() -> None:
    with _test_session() as db:
        owner, other = _seed_users(db)
        agent = AgentProfile(
            id="agent_private",
            tenant_id="tenant_demo",
            name="私有员工",
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        db.add(agent)
        db.commit()

        with pytest.raises(HTTPException) as access_error:
            get_agent_work_record(
                agent.id,
                tenant_id="tenant_demo",
                timezone="Asia/Shanghai",
                db=db,
                current_user=other,
            )
        assert access_error.value.status_code == 403

        with pytest.raises(HTTPException) as timezone_error:
            get_agent_work_record(
                agent.id,
                tenant_id="tenant_demo",
                timezone="Not/A_Timezone",
                db=db,
                current_user=owner,
            )
        assert timezone_error.value.status_code == 400


def _seed_users(db: Session) -> tuple[User, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    owner = User(
        id="user_owner",
        tenant_id="tenant_demo",
        username="owner",
        password_hash="x",
    )
    other = User(
        id="user_other",
        tenant_id="tenant_demo",
        username="other",
        password_hash="x",
    )
    db.add(owner)
    db.add(other)
    db.commit()
    return owner, other


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
