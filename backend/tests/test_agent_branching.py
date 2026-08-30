from __future__ import annotations

import sqlite3

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import (
    copy_overall_scope_to_agent,
    ensure_knowledge_base_version,
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    hide_open_gallery_binding,
    knowledge_version_for_upload,
    require_overall_agent,
    update_branch_skill,
    visible_knowledge_base_versions,
    visible_skill_rows,
)
from app.agents.schema import AgentResourceImportRequest
from app.api import agents as agents_api
from app.api.agents import _skill_branch_read, import_agent_resources, list_agents, list_chat_agents
from app.api.general_skills import archive_general_skill, list_general_skills
from app.api.knowledge_bases import list_knowledge_bases, update_knowledge_base
from app.api.skills import (
    archive_skill,
    delete_skill,
    get_skill,
    publish_skill,
    skill_read,
    update_skill,
)
from app.api.tools import list_tools
from app.db import staffdeck_seed
from app.db.models import (
    AgentKnowledgeBranch,
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    AgentUsage,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDocument,
    Skill,
    Tenant,
    Tool,
    User,
)
from app.db.seed import EXCHANGE_SKILL, REFUND_SKILL, _publish_seeded_system_resources
from app.knowledge.okf import upsert_concepts
from app.knowledge.schema import KnowledgeBaseUpdateRequest
from app.skills.skill_schema import SkillCard, SkillUpdateRequest


def _admin_user() -> User:
    return User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash="test",
    )


def test_management_and_chat_agent_lists_share_one_access_scope() -> None:
    with _test_session() as db:
        member = User(
            id="user_member",
            tenant_id="tenant_demo",
            username="member",
            role="member",
            password_hash="test",
        )
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(member)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_owned",
                tenant_id="tenant_demo",
                name="本人员工",
                metadata_json={"owner_user_id": member.id},
            )
        )
        db.add(
            AgentProfile(
                id="agent_gallery_unused",
                tenant_id="tenant_demo",
                name="未使用广场员工",
                metadata_json={"published_to_gallery": True, "owner_user_id": "other"},
            )
        )
        db.add(
            AgentProfile(
                id="agent_gallery_used",
                tenant_id="tenant_demo",
                name="已使用广场员工",
                metadata_json={"published_to_gallery": True, "owner_user_id": "other"},
            )
        )
        db.add(
            AgentProfile(
                id="agent_private_other",
                tenant_id="tenant_demo",
                name="他人私有员工",
                metadata_json={"owner_user_id": "other"},
            )
        )
        db.add(
            AgentUsage(
                tenant_id="tenant_demo",
                user_id=member.id,
                agent_id="agent_gallery_used",
            )
        )
        db.commit()

        management = list_agents("tenant_demo", db, member)
        chat = list_chat_agents("tenant_demo", member, db)
        management_by_id = {row.id: row for row in management}

        assert set(management_by_id) == {
            "agent_overall",
            "agent_owned",
            "agent_gallery_unused",
            "agent_gallery_used",
        }
        assert {row.id for row in chat} == {"agent_gallery_used", "agent_owned"}
        assert management_by_id["agent_gallery_unused"].metadata["used_by_current_user"] is False
        assert management_by_id["agent_gallery_used"].metadata["used_by_current_user"] is True


def test_seed_publishing_writes_explicit_admin_owner_without_backfilling_user_resources() -> None:
    with _test_session() as db:
        overall = AgentProfile(
            id="agent_tenant_demo_overall",
            tenant_id="tenant_demo",
            name="开放广场",
            is_overall=True,
        )
        default_agent = AgentProfile(
            id="agent_tenant_demo_default",
            tenant_id="tenant_demo",
            name="默认员工",
        )
        user_agent = AgentProfile(
            id="agent_user_owned",
            tenant_id="tenant_demo",
            name="用户员工",
            metadata_json={"owner_user_id": "user_member"},
        )
        seeded_active = Skill(
            id="skill_seed_active_row",
            tenant_id="tenant_demo",
            skill_id=str(REFUND_SKILL["skill_id"]),
            version="1.0.0",
            name="系统退款流程",
            content_json=dict(REFUND_SKILL),
            status="published",
        )
        seeded_deleted = Skill(
            id="skill_seed_deleted_row",
            tenant_id="tenant_demo",
            skill_id=str(EXCHANGE_SKILL["skill_id"]),
            version="1.0.0",
            name="已从广场删除的系统流程",
            content_json=dict(EXCHANGE_SKILL),
            status="published",
        )
        user_skill = Skill(
            id="skill_user_row",
            tenant_id="tenant_demo",
            skill_id="user_skill",
            version="1.0.0",
            name="用户流程",
            content_json={"skill_id": "user_skill", "name": "用户流程", "nodes": []},
            status="published",
        )
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(overall)
        db.add(default_agent)
        db.add(user_agent)
        db.add(seeded_active)
        db.add(seeded_deleted)
        db.add(user_skill)
        db.flush()
        hide_open_gallery_binding(db, "tenant_demo", "skill", seeded_deleted.id)
        db.commit()

        _publish_seeded_system_resources(db)
        db.commit()

        active_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == overall.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == seeded_active.id,
            )
        ).one()
        deleted_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == overall.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == seeded_deleted.id,
            )
        ).one()
        custom_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == overall.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == user_skill.id,
            )
        ).first()

        assert active_binding.status == "active"
        assert active_binding.metadata_json["owner_user_id"] == "admin"
        assert deleted_binding.status == "deleted"
        assert custom_binding is None
        assert overall.metadata_json["owner_user_id"] == "admin"
        assert default_agent.status == "archived"
        assert default_agent.metadata_json["hidden_from_staffdeck"] is True
        assert default_agent.metadata_json["owner_user_id"] == "admin"
        assert user_agent.metadata_json == {"owner_user_id": "user_member"}


def test_agent_skill_branch_is_copy_on_write_and_reports_branch_state() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        agent = AgentProfile(
            id="agent_branch", tenant_id="tenant_demo", name="客服分支", is_overall=False
        )
        skill = Skill(
            tenant_id="tenant_demo",
            skill_id="skill_purchase",
            version="1.0.0",
            name="购买流程",
            business_domain="电商",
            description="购买商品",
            status="published",
            content_json=_graph("购买流程", "1.0.0"),
        )
        db.add(agent)
        db.add(skill)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "skill", skill.id, "active")
        db.commit()

        copy_overall_scope_to_agent(db, "tenant_demo", agent)
        db.commit()

        visible = visible_skill_rows(db, "tenant_demo", agent.id)
        assert len(visible) == 1
        branch_read = _skill_branch_read(visible[0])
        assert branch_read["branch_sync_state"] == "synced"
        assert branch_read["branch_head_version"] == "1.0.0"

        update_branch_skill(
            db, "tenant_demo", agent.id, skill, _graph("分支购买流程", "1.0.0-branch.1")
        )
        db.commit()

        branch_visible = visible_skill_rows(db, "tenant_demo", agent.id)[0]
        global_skill = db.exec(select(Skill).where(Skill.skill_id == "skill_purchase")).first()
        assert branch_visible.name == "分支购买流程"
        assert global_skill is not None
        assert global_skill.name == "购买流程"
        assert _skill_branch_read(branch_visible)["branch_sync_state"] == "diverged"


def test_open_gallery_delete_skill_hides_gallery_without_removing_agent_binding() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_branch", tenant_id="tenant_demo", name="研发员工", is_overall=False
            )
        )
        skill = Skill(
            id="skill_weather_row",
            tenant_id="tenant_demo",
            skill_id="skill_weather",
            version="1.0.0",
            name="天气查询流程",
            business_domain="工具",
            description="查询天气",
            status="published",
            content_json=_graph("天气查询流程", "1.0.0"),
        )
        db.add(skill)
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id="agent_branch",
                resource_type="skill",
                resource_id=skill.id,
                status="active",
            )
        )
        db.commit()
        ensure_open_gallery_binding(db, "tenant_demo", "skill", skill.id, "active")
        db.commit()

        result = delete_skill(
            skill.skill_id,
            "tenant_demo",
            db,
            agent_id="agent_overall",
            current_user=_admin_user(),
        )

        assert result == {"status": "hidden"}
        assert db.get(Skill, skill.id) is not None
        branch_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == "agent_branch",
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).one()
        assert branch_binding.status == "active"
        assert visible_skill_rows(db, "tenant_demo", "agent_overall") == []
        assert visible_skill_rows(db, "tenant_demo", "agent_branch") == []


def test_open_gallery_deleted_skill_binding_is_not_restored_by_ensure() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        skill = Skill(
            id="skill_deleted_gallery_row",
            tenant_id="tenant_demo",
            skill_id="skill_deleted_gallery",
            version="1.0.0",
            name="已从广场移除的流程",
            business_domain="工具",
            description="删除后不应被旧同步逻辑恢复",
            status="published",
            content_json=_graph("已从广场移除的流程", "1.0.0"),
        )
        db.add(skill)
        db.commit()
        ensure_open_gallery_binding(db, "tenant_demo", "skill", skill.id, "active")
        db.commit()
        assert delete_skill(
            skill.skill_id,
            "tenant_demo",
            db,
            agent_id="agent_overall",
            current_user=_admin_user(),
        ) == {"status": "hidden"}

        ensure_open_gallery_binding(db, "tenant_demo", "skill", skill.id, "active")
        db.commit()

        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == "agent_overall",
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).one()
        assert binding.status == "deleted"
        assert visible_skill_rows(db, "tenant_demo", "agent_overall") == []


def test_open_gallery_skill_requires_explicit_overall_binding() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        skill = Skill(
            id="skill_without_gallery_binding_row",
            tenant_id="tenant_demo",
            skill_id="skill_without_gallery_binding",
            version="1.0.0",
            name="未开放流程",
            business_domain="工具",
            description="没有绑定就不属于开放广场",
            status="published",
            content_json=_graph("未开放流程", "1.0.0"),
        )
        db.add(skill)
        db.commit()

        assert visible_skill_rows(db, "tenant_demo", "agent_overall") == []


def test_open_gallery_skill_read_returns_persisted_creator_metadata() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        skill = Skill(
            id="skill_weather_row",
            tenant_id="tenant_demo",
            skill_id="skill_weather",
            version="1.0.0",
            name="天气查询流程",
            business_domain="工具",
            description="查询天气",
            status="published",
            content_json=_graph("天气查询流程", "1.0.0"),
        )
        db.add(skill)
        db.commit()
        ensure_open_gallery_binding(
            db,
            "tenant_demo",
            "skill",
            skill.id,
            "active",
            metadata_json={"creator_name": "admin", "created_by_username": "admin"},
        )
        db.commit()

        visible = visible_skill_rows(db, "tenant_demo", "agent_overall")
        read = skill_read(visible[0])
        branch_read = _skill_branch_read(visible[0])

        assert read.metadata["creator_name"] == "admin"
        assert read.metadata["created_by_username"] == "admin"
        assert branch_read["metadata"]["creator_name"] == "admin"
        assert branch_read["metadata"]["created_by_username"] == "admin"


def test_private_skill_branch_creator_metadata_is_written_from_agent_owner() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        agent = AgentProfile(
            id="agent_owner",
            tenant_id="tenant_demo",
            name="个人员工",
            is_overall=False,
            metadata_json={
                "owner_user_id": "user_owner",
                "owner_username": "owner",
                "created_by_user_id": "user_owner",
                "created_by_username": "owner",
                "created_by": "owner",
                "creator_name": "owner",
            },
        )
        skill = Skill(
            id="skill_private_row",
            tenant_id="tenant_demo",
            skill_id="skill_private",
            version="1.0.0",
            name="个人 SOP",
            business_domain="个人",
            description="个人创建",
            status="published",
            content_json=_graph("个人 SOP", "1.0.0"),
        )
        db.add(agent)
        db.add(skill)
        db.flush()
        ensure_private_resource_binding(db, "tenant_demo", agent.id, "skill", skill.id)
        db.commit()

        visible = visible_skill_rows(db, "tenant_demo", agent.id)
        read = skill_read(visible[0])
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == "tenant_demo",
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).first()

        assert read.metadata["creator_name"] == "owner"
        assert read.metadata["created_by_username"] == "owner"
        assert branch is not None
        assert branch.metadata_json["creator_name"] == "owner"


def test_list_agents_allows_tool_resource_bindings() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        agent = AgentProfile(
            id="agent_tool_owner", tenant_id="tenant_demo", name="工具员工", is_overall=False
        )
        tool = Tool(
            id="tool_lookup",
            tenant_id="tenant_demo",
            name="product.lookup",
            display_name="商品查询",
            method="POST",
            url="/api/mock/product/lookup",
        )
        db.add(agent)
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(db, "tenant_demo", agent.id, "tool", tool.id, "active")
        db.commit()

        result = list_agents("tenant_demo", db, _admin_user())

        assert result[0].resources[0].resource_type == "tool"
        assert result[0].resources[0].resource_id == tool.id


def test_copy_overall_scope_to_agent_does_not_auto_bind_open_gallery_tools() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        owner = AgentProfile(
            id="agent_owner", tenant_id="tenant_demo", name="个人员工", is_overall=False
        )
        target = AgentProfile(
            id="agent_target", tenant_id="tenant_demo", name="研发员工", is_overall=False
        )
        open_tool = Tool(
            id="tool_open_lookup",
            tenant_id="tenant_demo",
            name="product.lookup",
            display_name="商品查询",
            method="POST",
            url="/api/mock/product/lookup",
        )
        private_tool = Tool(
            id="tool_private_lookup",
            tenant_id="tenant_demo",
            name="private.lookup",
            display_name="个人查询",
            method="POST",
            url="/api/mock/private/lookup",
        )
        db.add(owner)
        db.add(target)
        db.add(open_tool)
        db.add(private_tool)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", open_tool.id, "active")
        ensure_private_resource_binding(
            db, "tenant_demo", owner.id, "tool", private_tool.id, "active"
        )
        db.commit()

        copy_overall_scope_to_agent(db, "tenant_demo", target)
        db.commit()

        bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == target.id,
                AgentResourceBinding.resource_type == "tool",
            )
        ).all()

        assert bindings == []
        visible_tools = list_tools(tenant_id="tenant_demo", bucket=None, agent_id=target.id, db=db)
        assert visible_tools == []
        open_tools = list_tools(
            tenant_id="tenant_demo", bucket=None, agent_id="agent_overall", db=db
        )
        assert [tool.id for tool in open_tools] == [open_tool.id]


def test_copy_overall_scope_to_agent_does_not_auto_bind_open_gallery_knowledge_bases() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        target = AgentProfile(
            id="agent_target", tenant_id="tenant_demo", name="研发员工", is_overall=False
        )
        kb = KnowledgeBase(
            id="kb_open_policy",
            tenant_id="tenant_demo",
            name="商品政策",
            status="active",
        )
        db.add(target)
        db.add(kb)
        db.flush()
        ensure_knowledge_base_version(db, kb)
        ensure_open_gallery_binding(db, "tenant_demo", "knowledge_base", kb.id, "active")
        db.commit()

        copy_overall_scope_to_agent(db, "tenant_demo", target)
        db.commit()

        bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == target.id,
                AgentResourceBinding.resource_type == "knowledge_base",
            )
        ).all()

        assert bindings == []
        assert visible_knowledge_base_versions(db, "tenant_demo", target.id) == {}
        assert list_knowledge_bases(tenant_id="tenant_demo", agent_id=target.id, db=db) == []
        open_knowledge = list_knowledge_bases(
            tenant_id="tenant_demo", agent_id="agent_overall", db=db
        )
        assert [row.id for row in open_knowledge] == [kb.id]


def test_legacy_dedicated_branch_binding_without_scope_metadata_remains_private_visible() -> None:
    """旧员工分支只凭 branch+binding 也必须可见，且不能因此进入开放广场。"""
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        overall = AgentProfile(
            id="agent_overall",
            tenant_id="tenant_demo",
            name="整体智能体",
            is_overall=True,
        )
        owner = AgentProfile(
            id="agent_owner",
            tenant_id="tenant_demo",
            name="资料员工",
            is_overall=False,
        )
        other = AgentProfile(
            id="agent_other",
            tenant_id="tenant_demo",
            name="其他员工",
            is_overall=False,
        )
        knowledge_base = KnowledgeBase(
            id="kb_legacy_dedicated",
            tenant_id="tenant_demo",
            name="旧版员工资料",
            mode="dedicated",
            status="active",
        )
        db.add(overall)
        db.add(owner)
        db.add(other)
        db.add(knowledge_base)
        db.flush()
        ensure_knowledge_base_version(db, knowledge_base, "1.0.0")
        db.add(
            AgentKnowledgeBranch(
                id="branch_legacy_dedicated",
                tenant_id="tenant_demo",
                agent_id=owner.id,
                knowledge_base_id=knowledge_base.id,
                base_version="1.0.0",
                head_version="1.0.0",
                status="active",
                sync_state="synced",
                metadata_json={},
            )
        )
        db.add(
            AgentResourceBinding(
                id="binding_legacy_dedicated",
                tenant_id="tenant_demo",
                agent_id=owner.id,
                resource_type="knowledge_base",
                resource_id=knowledge_base.id,
                status="active",
                metadata_json={},
            )
        )
        db.commit()

        assert set(visible_knowledge_base_versions(db, "tenant_demo", owner.id)) == {
            knowledge_base.id
        }
        assert visible_knowledge_base_versions(db, "tenant_demo", other.id) == {}
        assert visible_knowledge_base_versions(db, "tenant_demo", overall.id) == {}


def test_shared_knowledge_base_cannot_be_exposed_by_stale_open_gallery_binding() -> None:
    """共享知识库即使残留整体员工绑定，也只能走团队授权链路。"""
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        overall = AgentProfile(
            id="agent_overall",
            tenant_id="tenant_demo",
            name="整体智能体",
            is_overall=True,
        )
        shared = KnowledgeBase(
            id="kb_shared_not_gallery",
            tenant_id="tenant_demo",
            name="团队共享资料",
            mode="shared",
            status="active",
        )
        db.add(overall)
        db.add(shared)
        db.flush()
        released = ensure_knowledge_base_version(db, shared, "1.0.0")
        shared.published_version_id = released.id
        db.add(shared)
        ensure_open_gallery_binding(
            db,
            "tenant_demo",
            "knowledge_base",
            shared.id,
            "active",
        )
        db.commit()

        assert visible_knowledge_base_versions(db, "tenant_demo", overall.id) == {}


def test_staffdeck_gallery_seed_skips_shared_knowledge_bases() -> None:
    """内置开放广场种子不能把共享知识库重新发布成整体员工模板。"""
    with _test_session() as db:
        db.add(Tenant(id=staffdeck_seed.TENANT_ID, name="StaffDeck"))
        overall = AgentProfile(
            id="agent_staffdeck_overall",
            tenant_id=staffdeck_seed.TENANT_ID,
            name="整体智能体",
            is_overall=True,
        )
        shared = KnowledgeBase(
            id="kb_seed_shared",
            tenant_id=staffdeck_seed.TENANT_ID,
            name="共享知识库",
            mode="shared",
            status="active",
            metadata_json={"source": "team"},
        )
        dedicated = KnowledgeBase(
            id="kb_seed_dedicated",
            tenant_id=staffdeck_seed.TENANT_ID,
            name="专用知识库模板",
            mode="dedicated",
            status="active",
            metadata_json={"source": "seed"},
        )
        db.add(overall)
        db.add(shared)
        db.add(dedicated)
        db.flush()

        staffdeck_seed._publish_gallery_resources(
            db,
            {
                "skill": {},
                "general_skill": {},
                "tool": {},
                "knowledge_base": {
                    shared.id: shared.id,
                    dedicated.id: dedicated.id,
                },
            },
        )
        db.commit()

        assert db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == overall.id,
                AgentResourceBinding.resource_type == "knowledge_base",
                AgentResourceBinding.resource_id == shared.id,
            )
        ).first() is None
        assert db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == overall.id,
                AgentResourceBinding.resource_type == "knowledge_base",
                AgentResourceBinding.resource_id == dedicated.id,
                AgentResourceBinding.status == "active",
            )
        ).first() is not None
        db.refresh(shared)
        assert shared.metadata_json == {"source": "team"}


def test_list_agents_knowledge_count_ignores_stale_or_empty_default_bindings() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        agent = AgentProfile(
            id="agent_target", tenant_id="tenant_demo", name="研发员工", is_overall=False
        )
        stale_kb = KnowledgeBase(
            id="kb_stale",
            tenant_id="tenant_demo",
            name="有绑定但没有分支",
            status="active",
        )
        default_kb = KnowledgeBase(
            id="kb_default",
            tenant_id="tenant_demo",
            name="默认知识库",
            status="active",
        )
        db.add(agent)
        db.add(stale_kb)
        db.add(default_kb)
        db.flush()
        for kb in (stale_kb, default_kb):
            ensure_knowledge_base_version(db, kb)
            ensure_open_gallery_binding(db, "tenant_demo", "knowledge_base", kb.id, "active")
            db.add(
                AgentResourceBinding(
                    tenant_id="tenant_demo",
                    agent_id=agent.id,
                    resource_type="knowledge_base",
                    resource_id=kb.id,
                    status="active",
                )
            )
        db.add(
            AgentKnowledgeBranch(
                tenant_id="tenant_demo",
                agent_id=agent.id,
                knowledge_base_id=default_kb.id,
                status="active",
            )
        )
        db.commit()

        rows = list_agents("tenant_demo", db, _admin_user())
        target = next(row for row in rows if row.id == agent.id)

        assert target.resources == []


def test_agent_summary_resources_match_operational_resource_lists() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        agent = AgentProfile(
            id="agent_target", tenant_id="tenant_demo", name="研发员工", is_overall=False
        )
        skill = Skill(
            id="skill_visible",
            tenant_id="tenant_demo",
            skill_id="visible_sop",
            version="1.0.0",
            name="可用 SOP",
            status="published",
            content_json=_graph("可用 SOP", "1.0.0"),
        )
        archived_skill = Skill(
            id="skill_archived",
            tenant_id="tenant_demo",
            skill_id="archived_sop",
            version="1.0.0",
            name="已停用 SOP",
            status="archived",
            content_json=_graph("已停用 SOP", "1.0.0"),
        )
        general_skill = GeneralSkill(
            id="general_visible",
            tenant_id="tenant_demo",
            slug="visible-general-skill",
            name="可用技能",
            skill_markdown="# 可用技能",
            status="published",
        )
        archived_general_skill = GeneralSkill(
            id="general_archived",
            tenant_id="tenant_demo",
            slug="archived-general-skill",
            name="已停用技能",
            skill_markdown="# 已停用技能",
            status="archived",
        )
        tool = Tool(
            id="tool_visible",
            tenant_id="tenant_demo",
            name="visible.tool",
            method="POST",
            url="/api/mock/visible",
            enabled=True,
        )
        disabled_tool = Tool(
            id="tool_disabled",
            tenant_id="tenant_demo",
            name="disabled.tool",
            method="POST",
            url="/api/mock/disabled",
            enabled=False,
        )
        kb = KnowledgeBase(
            id="kb_visible", tenant_id="tenant_demo", name="可用知识库", status="active"
        )
        db.add(agent)
        for resource in (
            skill,
            archived_skill,
            general_skill,
            archived_general_skill,
            tool,
            disabled_tool,
            kb,
        ):
            db.add(resource)
        db.flush()
        for resource_type, resource_id in (
            ("skill", skill.id),
            ("skill", archived_skill.id),
            ("skill", "missing_skill"),
            ("general_skill", general_skill.id),
            ("general_skill", archived_general_skill.id),
            ("general_skill", "missing_general_skill"),
            ("tool", tool.id),
            ("tool", disabled_tool.id),
            ("tool", "missing_tool"),
            ("knowledge_base", kb.id),
            ("knowledge_base", "missing_kb"),
        ):
            ensure_private_resource_binding(
                db, "tenant_demo", agent.id, resource_type, resource_id, "active"
            )
        ensure_knowledge_base_version(db, kb)
        db.add(
            AgentKnowledgeBranch(
                tenant_id="tenant_demo",
                agent_id=agent.id,
                knowledge_base_id=kb.id,
                status="active",
            )
        )
        db.commit()

        summary = next(
            row for row in list_agents("tenant_demo", db, _admin_user()) if row.id == agent.id
        )
        summary_ids = {
            (row.resource_type, row.resource_id)
            for row in summary.resources
            if row.status == "active"
        }

        assert summary_ids == {
            ("skill", skill.id),
            ("general_skill", general_skill.id),
            ("tool", tool.id),
            ("knowledge_base", kb.id),
        }
        assert [
            row.id
            for row in visible_skill_rows(db, "tenant_demo", agent.id, include_inactive=False)
        ] == [skill.id]
        assert [
            row.id
            for row in list_general_skills("tenant_demo", db, agent.id)
            if row.status == "published"
        ] == [general_skill.id]
        assert [row.id for row in list_tools("tenant_demo", None, agent.id, db) if row.enabled] == [
            tool.id
        ]
        assert set(visible_knowledge_base_versions(db, "tenant_demo", agent.id)) == {kb.id}


def test_archiving_open_gallery_resources_updates_binding_status() -> None:
    with _test_session() as db:
        admin = _admin_user()
        db.add(Tenant(id="tenant_demo", name="Demo"))
        overall = AgentProfile(
            id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
        )
        skill = Skill(
            id="skill_archive",
            tenant_id="tenant_demo",
            skill_id="archive_sop",
            version="1.0.0",
            name="待停用 SOP",
            status="published",
            content_json=_graph("待停用 SOP", "1.0.0"),
        )
        general_skill = GeneralSkill(
            id="general_archive",
            tenant_id="tenant_demo",
            slug="archive-general-skill",
            name="待停用技能",
            skill_markdown="# 待停用技能",
            status="published",
        )
        kb = KnowledgeBase(
            id="kb_archive", tenant_id="tenant_demo", name="待停用知识库", status="active"
        )
        db.add(overall)
        db.add(skill)
        db.add(general_skill)
        db.add(kb)
        db.flush()
        for resource_type, resource_id in (
            ("skill", skill.id),
            ("general_skill", general_skill.id),
            ("knowledge_base", kb.id),
        ):
            ensure_open_gallery_binding(db, "tenant_demo", resource_type, resource_id, "active")
        db.commit()

        archive_skill(skill.skill_id, "tenant_demo", overall.id, db, admin)
        archive_general_skill(general_skill.slug, "tenant_demo", db, overall.id, admin)
        update_knowledge_base(
            kb.id,
            KnowledgeBaseUpdateRequest(tenant_id="tenant_demo", status="archived"),
            overall.id,
            db,
            admin,
        )

        bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == overall.id,
            )
        ).all()
        assert {(row.resource_type, row.status) for row in bindings} == {
            ("skill", "inactive"),
            ("general_skill", "inactive"),
            ("knowledge_base", "inactive"),
        }


def test_import_open_gallery_tool_creates_private_agent_binding() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        overall = AgentProfile(
            id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
        )
        target = AgentProfile(
            id="agent_target", tenant_id="tenant_demo", name="研发员工", is_overall=False
        )
        tool = Tool(
            id="tool_open_lookup",
            tenant_id="tenant_demo",
            name="product.lookup",
            display_name="商品查询",
            method="POST",
            url="/api/mock/product/lookup",
            enabled=True,
        )
        db.add(overall)
        db.add(target)
        db.add(tool)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id, "active")
        db.commit()

        result = import_agent_resources(
            target.id,
            AgentResourceImportRequest(
                tenant_id="tenant_demo",
                source_agent_id=overall.id,
                resource_type="tool",
                resource_ids=[tool.id],
            ),
            db,
            current_user=_admin_user(),
        )

        assert result["missing"] == []
        assert result["imported"] == [
            {
                "resource_type": "tool",
                "resource_id": tool.id,
                "display_id": tool.name,
                "name": tool.name,
            }
        ]
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == target.id,
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == tool.id,
            )
        ).one()
        assert binding.metadata_json["scope"] == "agent_private"
        assert binding.metadata_json["visibility"] == "agent_private"
        assert binding.metadata_json["owner_agent_id"] == target.id

        hidden = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == overall.id,
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == tool.id,
            )
        ).one()
        hidden.status = "deleted"
        db.add(hidden)
        db.commit()

        assert list_tools(tenant_id="tenant_demo", bucket=None, agent_id=overall.id, db=db) == []
        visible_tools = list_tools(tenant_id="tenant_demo", bucket=None, agent_id=target.id, db=db)
        assert [row.id for row in visible_tools] == [tool.id]


def test_import_resources_retries_once_when_sqlite_database_is_locked(monkeypatch) -> None:
    calls = 0

    class FakeSession:
        rollback_count = 0

        def rollback(self) -> None:
            self.rollback_count += 1

    expected = {"status": "imported", "imported": [], "missing": []}

    def fake_import_once(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError(
                "UPDATE agent_resource_bindings",
                {},
                sqlite3.OperationalError("database is locked"),
            )
        return expected

    fake_db = FakeSession()
    monkeypatch.setattr(agents_api, "_import_agent_resources_once", fake_import_once)
    monkeypatch.setattr(agents_api, "sleep", lambda _seconds: None)

    result = agents_api.import_agent_resources(
        "agent_target",
        AgentResourceImportRequest(
            tenant_id="tenant_demo",
            source_agent_id="agent_overall",
            resource_type="skill",
            resource_ids=["skill_demo"],
        ),
        fake_db,  # type: ignore[arg-type]
        current_user=_admin_user(),
    )

    assert result == expected
    assert calls == 2
    assert fake_db.rollback_count == 1


def test_non_overall_agent_cannot_delete_global_resources() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_branch", tenant_id="tenant_demo", name="客服分支", is_overall=False
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            require_overall_agent(db, "tenant_demo", "agent_branch")

        assert exc_info.value.status_code == 403


def test_management_rows_keep_archived_global_and_inactive_branch_skills() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        agent = AgentProfile(
            id="agent_branch", tenant_id="tenant_demo", name="客服分支", is_overall=False
        )
        global_archived = Skill(
            tenant_id="tenant_demo",
            skill_id="global_archived",
            version="1.0.0",
            name="主干下线技能",
            business_domain="电商",
            description="已下线但仍应管理可见",
            status="archived",
            content_json=_graph("主干下线技能", "1.0.0"),
        )
        branch_skill = Skill(
            tenant_id="tenant_demo",
            skill_id="branch_inactive",
            version="1.0.0",
            name="分支下线技能",
            business_domain="电商",
            description="分支下线但仍应管理可见",
            status="published",
            content_json=_graph("分支下线技能", "1.0.0"),
        )
        db.add(agent)
        db.add(global_archived)
        db.add(branch_skill)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "skill", global_archived.id, "inactive")
        ensure_open_gallery_binding(db, "tenant_demo", "skill", branch_skill.id, "active")
        db.commit()

        copy_overall_scope_to_agent(db, "tenant_demo", agent)
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == "tenant_demo",
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == branch_skill.skill_id,
            )
        ).one()
        branch.status = "inactive"
        db.add(branch)
        db.commit()

        overall_ids = {row.skill_id for row in visible_skill_rows(db, "tenant_demo")}
        branch_rows = visible_skill_rows(db, "tenant_demo", agent.id)
        branch_by_id = {row.skill_id: row for row in branch_rows}

        assert "global_archived" in overall_ids
        assert branch_by_id["branch_inactive"].status == "archived"


def test_updating_inactive_branch_skill_keeps_it_inactive() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        agent = AgentProfile(
            id="agent_branch", tenant_id="tenant_demo", name="客服分支", is_overall=False
        )
        skill = Skill(
            tenant_id="tenant_demo",
            skill_id="branch_inactive_edit",
            version="1.0.0",
            name="停用分支技能",
            business_domain="电商",
            description="停用后仍应支持编辑",
            status="published",
            content_json=_graph("停用分支技能", "1.0.0"),
        )
        db.add(agent)
        db.add(skill)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "skill", skill.id, "active")
        db.commit()

        copy_overall_scope_to_agent(db, "tenant_demo", agent)
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == "tenant_demo",
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).one()
        branch.status = "inactive"
        db.add(branch)
        db.commit()

        updated = update_branch_skill(
            db,
            "tenant_demo",
            agent.id,
            skill,
            _graph("停用分支技能已编辑", "1.0.1"),
        )
        db.commit()

        assert updated.status == "inactive"
        assert updated.sync_state == "diverged"
        visible = visible_skill_rows(db, "tenant_demo", agent.id)
        branch_read = _skill_branch_read(
            next(row for row in visible if row.skill_id == skill.skill_id)
        )
        assert branch_read["status"] == "archived"
        assert branch_read["branch_status"] == "inactive"
        assert branch_read["name"] == "停用分支技能已编辑"


def test_inactive_bound_skill_can_be_loaded_and_saved_from_management_api() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        agent = AgentProfile(
            id="agent_branch", tenant_id="tenant_demo", name="客服分支", is_overall=False
        )
        skill_content = _graph("停用分支技能", "1.0.0")
        skill_content["skill_id"] = "branch_inactive_api_edit"
        skill = Skill(
            tenant_id="tenant_demo",
            skill_id="branch_inactive_api_edit",
            version="1.0.0",
            name="停用分支技能",
            business_domain="电商",
            description="停用后仍应支持从管理页编辑",
            status="published",
            content_json=skill_content,
        )
        db.add(agent)
        db.add(skill)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "skill", skill.id, "active")
        db.commit()

        copy_overall_scope_to_agent(db, "tenant_demo", agent)
        db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == "tenant_demo",
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).one()
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).one()
        binding.status = "inactive"
        db.add(binding)
        db.commit()

        loaded = get_skill(skill.skill_id, "tenant_demo", agent.id, db)

        assert loaded.status == "archived"
        assert loaded.branch_status == "active"

        edited_content = loaded.content.model_copy(deep=True)
        edited_content.name = "停用分支技能已编辑"
        edited_content.description = "停用状态下保存的新说明"

        saved = update_skill(
            skill.skill_id,
            SkillUpdateRequest(
                tenant_id="tenant_demo",
                content=SkillCard.model_validate(edited_content),
                status=loaded.status,
            ),
            agent.id,
            db,
            _admin_user(),
        )

        assert saved.status == "archived"
        assert saved.branch_status == "active"
        assert saved.name == "停用分支技能已编辑"
        branch_after = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == "tenant_demo",
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).one()
        binding_after = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).one()
        assert branch_after.status == "active"
        assert binding_after.status == "inactive"
        assert branch_after.content_json["description"] == "停用状态下保存的新说明"


def test_inactive_bound_skill_can_be_reenabled_from_management_api() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        agent = AgentProfile(
            id="agent_branch", tenant_id="tenant_demo", name="客服分支", is_overall=False
        )
        skill_content = _graph("可重新启用分支技能", "1.0.0")
        skill_content["skill_id"] = "branch_reenable_api"
        skill = Skill(
            tenant_id="tenant_demo",
            skill_id="branch_reenable_api",
            version="1.0.0",
            name="可重新启用分支技能",
            business_domain="电商",
            description="停用后应能重新启用",
            status="published",
            content_json=skill_content,
        )
        db.add(agent)
        db.add(skill)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "skill", skill.id, "active")
        db.commit()

        copy_overall_scope_to_agent(db, "tenant_demo", agent)
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == "tenant_demo",
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).one()
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).one()
        branch.status = "inactive"
        binding.status = "inactive"
        db.add(branch)
        db.add(binding)
        db.commit()

        published = publish_skill(skill.skill_id, "tenant_demo", agent.id, db, _admin_user())

        assert published.status == "published"
        assert published.branch_status == "active"
        binding_after = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).one()
        assert binding_after.status == "active"
        listed = visible_skill_rows(db, "tenant_demo", agent.id)
        assert next(row for row in listed if row.skill_id == skill.skill_id).status == "published"


def test_disabled_open_gallery_resources_cannot_be_learned() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        overall = AgentProfile(
            id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
        )
        target = AgentProfile(
            id="agent_branch", tenant_id="tenant_demo", name="客服分支", is_overall=False
        )
        db.add(overall)
        db.add(target)
        archived_skill = Skill(
            id="skill_archived",
            tenant_id="tenant_demo",
            skill_id="archived_sop",
            version="1.0.0",
            name="已停用 SOP",
            business_domain="电商",
            description="停用后不可学习",
            status="archived",
            content_json=_graph("已停用 SOP", "1.0.0"),
        )
        archived_general_skill = GeneralSkill(
            id="general_archived",
            tenant_id="tenant_demo",
            slug="archived-general-skill",
            name="已停用通用技能",
            skill_markdown="# 已停用通用技能",
            status="archived",
        )
        archived_knowledge_base = KnowledgeBase(
            id="kb_archived",
            tenant_id="tenant_demo",
            name="已停用业务资料",
            status="archived",
        )
        db.add(archived_skill)
        db.add(archived_general_skill)
        db.add(archived_knowledge_base)
        db.commit()

        for resource_type, resource_id in [
            ("skill", archived_skill.id),
            ("general_skill", archived_general_skill.id),
            ("knowledge_base", archived_knowledge_base.id),
        ]:
            result = import_agent_resources(
                target.id,
                AgentResourceImportRequest(
                    tenant_id="tenant_demo",
                    source_agent_id=overall.id,
                    resource_type=resource_type,  # type: ignore[arg-type]
                    resource_ids=[resource_id],
                ),
                db,
                current_user=_admin_user(),
            )

            assert result["imported"] == []
            assert result["missing"] == [
                {"resource_id": resource_id, "reason": "disabled_in_open_gallery"}
            ]

            assert (
                db.exec(
                    select(AgentResourceBinding).where(
                        AgentResourceBinding.tenant_id == "tenant_demo",
                        AgentResourceBinding.agent_id == target.id,
                        AgentResourceBinding.resource_type == resource_type,
                        AgentResourceBinding.resource_id == resource_id,
                    )
                ).first()
                is None
            )

        inherited = AgentProfile(
            id="agent_inherited", tenant_id="tenant_demo", name="继承分支", is_overall=False
        )
        db.add(inherited)
        db.flush()
        copy_overall_scope_to_agent(db, "tenant_demo", inherited)

        assert (
            db.exec(
                select(AgentResourceBinding).where(AgentResourceBinding.agent_id == inherited.id)
            ).all()
            == []
        )


def test_archived_knowledge_remains_manageable_but_is_not_runtime_visible() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        overall = AgentProfile(
            id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
        )
        target = AgentProfile(
            id="agent_target", tenant_id="tenant_demo", name="研发员工", is_overall=False
        )
        db.add(overall)
        db.add(target)
        kb = KnowledgeBase(
            id="kb_archived_manageable",
            tenant_id="tenant_demo",
            name="已停用业务资料",
            status="active",
        )
        db.add(kb)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "knowledge_base", kb.id, "active")
        db.commit()

        imported = import_agent_resources(
            target.id,
            AgentResourceImportRequest(
                tenant_id="tenant_demo",
                source_agent_id=overall.id,
                resource_type="knowledge_base",
                resource_ids=[kb.id],
            ),
            db,
            current_user=_admin_user(),
        )
        assert [item["resource_id"] for item in imported["imported"]] == [kb.id]

        kb.status = "archived"
        db.add(kb)
        db.commit()

        assert visible_knowledge_base_versions(db, "tenant_demo", overall.id) == {}
        assert visible_knowledge_base_versions(db, "tenant_demo", target.id) == {}
        overall_managed = visible_knowledge_base_versions(
            db,
            "tenant_demo",
            overall.id,
            include_inactive=True,
        )
        target_managed = visible_knowledge_base_versions(
            db,
            "tenant_demo",
            target.id,
            include_inactive=True,
        )
        assert list(overall_managed) == [kb.id]
        assert list(target_managed) == [kb.id]
        assert overall_managed[kb.id].status == "active"


def test_private_agent_resources_are_not_visible_in_open_gallery() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        overall = AgentProfile(
            id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
        )
        owner = AgentProfile(
            id="agent_owner", tenant_id="tenant_demo", name="个人员工", is_overall=False
        )
        target = AgentProfile(
            id="agent_target", tenant_id="tenant_demo", name="学习员工", is_overall=False
        )
        private_skill = Skill(
            id="skill_private",
            tenant_id="tenant_demo",
            skill_id="private_sop",
            version="1.0.0",
            name="个人 SOP",
            business_domain="电商",
            description="个人创建的 SOP",
            status="published",
            content_json=_graph("个人 SOP", "1.0.0"),
        )
        private_general_skill = GeneralSkill(
            id="general_private",
            tenant_id="tenant_demo",
            slug="private-general-skill",
            name="个人通用技能",
            skill_markdown="# 个人通用技能",
            status="published",
        )
        private_knowledge_base = KnowledgeBase(
            id="kb_private",
            tenant_id="tenant_demo",
            name="个人业务资料",
            status="active",
        )
        private_tool = Tool(
            id="tool_private",
            tenant_id="tenant_demo",
            name="private_tool",
            display_name="个人工具",
            description="个人工具",
            method="POST",
            url="mock://private",
            enabled=True,
        )
        db.add(overall)
        db.add(owner)
        db.add(target)
        db.add(private_skill)
        db.add(private_general_skill)
        db.add(private_knowledge_base)
        db.add(private_tool)
        db.flush()

        for resource_type, resource_id in [
            ("skill", private_skill.id),
            ("general_skill", private_general_skill.id),
            ("knowledge_base", private_knowledge_base.id),
            ("tool", private_tool.id),
        ]:
            ensure_private_resource_binding(
                db, "tenant_demo", owner.id, resource_type, resource_id, "active"
            )

        legacy_private_knowledge_base = KnowledgeBase(
            id="kb_legacy_private",
            tenant_id="tenant_demo",
            name="旧版个人上传资料",
            status="active",
            metadata_json={"created_from_document_upload": True},
        )
        db.add(legacy_private_knowledge_base)
        db.flush()
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id=owner.id,
                resource_type="knowledge_base",
                resource_id=legacy_private_knowledge_base.id,
                status="active",
                metadata_json={"created_from_agent": True, "created_from_upload": True},
            )
        )
        db.commit()

        for resource_type, resource_id in [
            ("skill", private_skill.id),
            ("general_skill", private_general_skill.id),
            ("knowledge_base", private_knowledge_base.id),
        ]:
            result = import_agent_resources(
                target.id,
                AgentResourceImportRequest(
                    tenant_id="tenant_demo",
                    source_agent_id=overall.id,
                    resource_type=resource_type,  # type: ignore[arg-type]
                    resource_ids=[resource_id],
                ),
                db,
                current_user=_admin_user(),
            )

            assert result["imported"] == []
            assert result["missing"] == [
                {"resource_id": resource_id, "reason": "disabled_in_open_gallery"}
            ]

        open_tools = list_tools(
            tenant_id="tenant_demo",
            bucket=None,
            agent_id=overall.id,
            db=db,
        )
        assert [row.id for row in open_tools] == []
        open_knowledge_versions = visible_knowledge_base_versions(db, "tenant_demo", overall.id)
        assert "kb_legacy_private" not in open_knowledge_versions


def test_import_private_general_skill_creates_independent_package_copy() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        source = AgentProfile(
            id="agent_private_skill_source",
            tenant_id="tenant_demo",
            name="私有技能源员工",
            is_overall=False,
        )
        target = AgentProfile(
            id="agent_private_skill_target",
            tenant_id="tenant_demo",
            name="私有技能目标员工",
            is_overall=False,
        )
        skill = GeneralSkill(
            id="general_private_package",
            tenant_id="tenant_demo",
            slug="private-package",
            name="私有目录技能",
            skill_markdown="# 私有目录技能\n",
            skill_files_json=[
                {"path": "SKILL.md", "content": "# 私有目录技能\n"},
                {"path": "references/details.md", "content": "# 详细说明\n"},
            ],
            metadata_json={
                "scope": "agent_private",
                "visibility": "agent_private",
                "owner_agent_id": source.id,
            },
            status="published",
        )
        db.add(source)
        db.add(target)
        db.add(skill)
        db.flush()
        ensure_private_resource_binding(
            db,
            "tenant_demo",
            source.id,
            "general_skill",
            skill.id,
            "active",
        )
        db.commit()

        result = import_agent_resources(
            target.id,
            AgentResourceImportRequest(
                tenant_id="tenant_demo",
                source_agent_id=source.id,
                resource_type="general_skill",
                resource_ids=[skill.id],
            ),
            db,
            current_user=_admin_user(),
        )

        copied_id = str(result["imported"][0]["resource_id"])
        copied = db.get(GeneralSkill, copied_id)
        assert copied is not None
        assert copied.id != skill.id
        assert copied.skill_files_json == skill.skill_files_json
        assert copied.metadata_json["owner_agent_id"] == target.id
        assert copied.metadata_json["copied_from_general_skill_id"] == skill.id
        target_bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == target.id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.status == "active",
            )
        ).all()
        assert [binding.resource_id for binding in target_bindings] == [copied.id]


def test_knowledge_branch_write_clones_existing_wiki_before_appending_concept() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        agent = AgentProfile(
            id="agent_branch", tenant_id="tenant_demo", name="客服分支", is_overall=False
        )
        kb = KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="业务资料")
        db.add(agent)
        db.add(kb)
        base_version = ensure_knowledge_base_version(db, kb, "1.0.0")
        document = KnowledgeDocument(
            id="doc_base",
            tenant_id="tenant_demo",
            knowledge_base_id=kb.id,
            knowledge_base_version_id=base_version.id,
            filename="policy.md",
            file_type="md",
            title="政策文档",
            status="ready",
            bucket_count=1,
            chunk_count=1,
        )
        bucket = KnowledgeBucket(
            id="bucket_base",
            tenant_id="tenant_demo",
            knowledge_base_id=kb.id,
            knowledge_base_version_id=base_version.id,
            document_id=document.id,
            bucket_key="policy",
            title="政策桶",
            summary="政策摘要",
        )
        chunk = KnowledgeChunk(
            id="chunk_base",
            tenant_id="tenant_demo",
            knowledge_base_id=kb.id,
            knowledge_base_version_id=base_version.id,
            document_id=document.id,
            bucket_id=bucket.id,
            chunk_index=0,
            content="用户取消订单前需要确认当前订单状态。",
        )
        concept = KnowledgeConcept(
            tenant_id="tenant_demo",
            knowledge_base_id=kb.id,
            knowledge_base_version_id=base_version.id,
            document_id=document.id,
            concept_id="playbooks/order-cancel",
            concept_type="Playbook",
            title="订单取消",
            description="订单取消流程",
            content_md="---\ntype: Playbook\ntitle: 订单取消\n---\n\n# Summary\n确认订单状态。",
        )
        db.add(document)
        db.add(bucket)
        db.add(chunk)
        db.add(concept)
        db.commit()

        target_version = knowledge_version_for_upload(db, "tenant_demo", kb.id, agent.id)
        upsert_concepts(
            db,
            "tenant_demo",
            kb.id,
            target_version.id,
            [
                {
                    "concept_id": "topics/new-topic",
                    "content_md": "---\ntype: Topic\ntitle: 新 Wiki 页面\n---\n\n# Summary\n补充新主题。",
                    "document_id": document.id,
                    "status": "active",
                }
            ],
        )

        concepts = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.knowledge_base_version_id == target_version.id
            )
        ).all()
        assert {row.concept_id for row in concepts} == {
            "playbooks/order-cancel",
            "topics/new-topic",
        }
        cloned_documents = db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == target_version.id
            )
        ).all()
        cloned_buckets = db.exec(
            select(KnowledgeBucket).where(
                KnowledgeBucket.knowledge_base_version_id == target_version.id
            )
        ).all()
        cloned_chunks = db.exec(
            select(KnowledgeChunk).where(
                KnowledgeChunk.knowledge_base_version_id == target_version.id
            )
        ).all()
        assert len(cloned_documents) == 1
        assert len(cloned_buckets) == 1
        assert len(cloned_chunks) == 1
        assert cloned_documents[0].id != document.id
        assert cloned_buckets[0].document_id == cloned_documents[0].id
        assert cloned_chunks[0].document_id == cloned_documents[0].id
        assert cloned_chunks[0].bucket_id == cloned_buckets[0].id


def test_knowledge_branch_write_normalizes_nested_branch_base_version() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        agent = AgentProfile(
            id="agent_branch", tenant_id="tenant_demo", name="客服分支", is_overall=False
        )
        kb = KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="业务资料")
        db.add(agent)
        db.add(kb)
        ensure_knowledge_base_version(db, kb, "1.0.0-branch.agent_branch.1")
        branch = AgentKnowledgeBranch(
            tenant_id="tenant_demo",
            agent_id=agent.id,
            knowledge_base_id=kb.id,
            base_version="1.0.0-branch.agent_branch.1",
            head_version="1.0.0-branch.agent_branch.1",
            status="active",
            sync_state="diverged",
        )
        db.add(branch)
        db.commit()

        target_version = knowledge_version_for_upload(db, "tenant_demo", kb.id, agent.id)

        assert target_version.version == "1.0.0-branch.agent_branch.2"
        assert branch.base_version == "1.0.0"


def _graph(name: str, version: str) -> dict[str, object]:
    return {
        "skill_id": "skill_purchase",
        "version": version,
        "name": name,
        "business_domain": "电商",
        "description": "购买商品",
        "nodes": [
            {
                "node_id": "collect",
                "type": "collect_info",
                "name": "收集信息",
                "instruction": "收集用户信息",
                "expected_user_info": ["user_name"],
                "allowed_actions": ["ask_user", "continue_flow"],
            },
            {
                "node_id": "reply",
                "type": "response",
                "name": "回复用户",
                "instruction": "回复用户",
                "allowed_actions": ["answer_user"],
            },
        ],
        "edges": [{"source_node_id": "collect", "next_node_id": "reply"}],
        "start_node_id": "collect",
        "terminal_node_ids": ["reply"],
    }


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
