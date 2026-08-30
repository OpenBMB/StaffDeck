from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import (
    ensure_agent_private_knowledge_branch,
    ensure_knowledge_base_version,
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    mark_resource_private_for_agent,
)
from app.api.general_skills import general_skill_read, import_general_skill
from app.api.knowledge_bases import (
    create_knowledge_base,
    knowledge_base_read,
    update_knowledge_base,
)
from app.api.skills import _with_available_context_for_distill
from app.api.tools import (
    _update_inherited_mcp_tool_scopes,
    create_tool,
    sync_mcp_tools,
    tool_read,
    update_tool,
)
from app.capability_scope import normalize_capability_scope
from app.core.capability_manifest import CapabilityManifestBuilder
from app.db.database import _migrate_capability_scope_schema
from app.db.models import (
    AgentKnowledgeBranch,
    AgentProfile,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBaseVersion,
    MCPServer,
    Team,
    TeamKnowledgeBaseBinding,
    TeamKnowledgeBaseGrant,
    TeamMember,
    Tenant,
    Tool,
    User,
)
from app.general_skills.schema import GeneralSkillImportRequest
from app.knowledge.schema import KnowledgeBaseCreateRequest, KnowledgeBaseUpdateRequest
from app.skills.skill_schema import SkillDistillRequest
from app.tools.tool_schema import (
    MCPServerCreateRequest,
    MCPSyncRequest,
    ToolCreateRequest,
    ToolUpdateRequest,
)


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _admin_user() -> User:
    return User(
        id="admin",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash="test",
    )


def test_distill_catalog_uses_capabilities_visible_to_selected_agent() -> None:
    with _test_session() as db:
        tenant = Tenant(id="tenant_demo", name="Demo")
        agent = AgentProfile(id="agent_writer", tenant_id=tenant.id, name="Writer")
        other_agent = AgentProfile(id="agent_other", tenant_id=tenant.id, name="Other")
        visible_tool = Tool(
            id="tool_excel",
            tenant_id=tenant.id,
            name="excel.write",
            display_name="写入 Excel",
            description="把结构化数据写入工作簿",
            method="POST",
            url="https://example.test/excel",
        )
        hidden_tool = Tool(
            id="tool_news",
            tenant_id=tenant.id,
            name="news.search",
            display_name="新闻检索",
            method="POST",
            url="https://example.test/news",
        )
        visible_skill = GeneralSkill(
            id="genskill_table",
            tenant_id=tenant.id,
            slug="table-cleanup",
            name="表格清洗",
            skill_markdown="# 表格清洗",
            status="published",
        )
        hidden_skill = GeneralSkill(
            id="genskill_news",
            tenant_id=tenant.id,
            slug="news-digest",
            name="新闻摘要",
            skill_markdown="# 新闻摘要",
            status="published",
        )
        mark_resource_private_for_agent(visible_tool, agent.id)
        mark_resource_private_for_agent(hidden_tool, other_agent.id)
        mark_resource_private_for_agent(visible_skill, agent.id)
        mark_resource_private_for_agent(hidden_skill, other_agent.id)
        db.add_all(
            [
                tenant,
                agent,
                other_agent,
                visible_tool,
                hidden_tool,
                visible_skill,
                hidden_skill,
            ]
        )
        db.flush()
        ensure_private_resource_binding(db, tenant.id, agent.id, "tool", visible_tool.id)
        ensure_private_resource_binding(
            db,
            tenant.id,
            agent.id,
            "general_skill",
            visible_skill.id,
        )
        db.commit()

        enriched = _with_available_context_for_distill(
            db,
            SkillDistillRequest(
                tenant_id=tenant.id,
                agent_id=agent.id,
                title="日报生成",
                raw_content="获取数据并写入 Excel",
            ),
        )

        assert {item["id"] for item in enriched.available_tools} == {visible_tool.id}
        assert {item["id"] for item in enriched.available_general_skills} == {
            visible_skill.id
        }


def test_capability_scope_request_defaults_and_update_compatibility() -> None:
    assert GeneralSkillImportRequest(tenant_id="tenant_demo", markdown="# demo").capability_scope is None
    assert (
        KnowledgeBaseCreateRequest(tenant_id="tenant_demo", name="制度库").capability_scope
        == "general"
    )
    assert (
        ToolCreateRequest(
            tenant_id="tenant_demo",
            name="demo.lookup",
            url="https://example.test",
        ).capability_scope
        == "general"
    )
    assert (
        MCPServerCreateRequest(tenant_id="tenant_demo", name="demo").capability_scope
        == "general"
    )
    assert KnowledgeBaseUpdateRequest(tenant_id="tenant_demo").capability_scope is None
    assert (
        ToolUpdateRequest(
            tenant_id="tenant_demo",
            name="demo.lookup",
            url="https://example.test",
        ).capability_scope
        is None
    )
    with pytest.raises(ValidationError):
        KnowledgeBaseCreateRequest(
            tenant_id="tenant_demo",
            name="非法范围",
            capability_scope="unsupported",  # type: ignore[arg-type]
        )


def test_team_manifest_freezes_the_global_shared_version_for_one_turn() -> None:
    """群聊冻结员工专用与团队共享读版本，但共享维护动作只携带共享库。"""
    with _test_session() as db:
        agent = AgentProfile(
            id="agent_writer",
            tenant_id="tenant_demo",
            name="内容员工",
        )
        team = Team(
            id="team_a",
            tenant_id="tenant_demo",
            name="项目 A",
            owner_user_id="user_admin",
        )
        knowledge_base = KnowledgeBase(
            id="kb_shared",
            tenant_id="tenant_demo",
            name="团队内容库",
            mode="shared",
            published_version_id="kbver_1",
        )
        version_1 = KnowledgeBaseVersion(
            id="kbver_1",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            version="1.0.0",
            name=knowledge_base.name,
            publication_state="released",
        )
        version_2 = KnowledgeBaseVersion(
            id="kbver_2",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            version="2.0.0",
            name=knowledge_base.name,
            parent_version_id=version_1.id,
            publication_state="released",
        )
        dedicated_base = KnowledgeBase(
            id="kb_writer_dedicated",
            tenant_id="tenant_demo",
            name="内容员工专用知识",
            mode="dedicated",
        )
        db.add_all(
            [
                agent,
                team,
                TeamMember(team_id=team.id, agent_id=agent.id),
                knowledge_base,
                version_1,
                version_2,
                dedicated_base,
                TeamKnowledgeBaseBinding(
                    tenant_id="tenant_demo",
                    team_id=team.id,
                    knowledge_base_id=knowledge_base.id,
                    created_by_user_id="user_admin",
                ),
                TeamKnowledgeBaseGrant(
                    tenant_id="tenant_demo",
                    team_id=team.id,
                    knowledge_base_id=knowledge_base.id,
                    agent_id=agent.id,
                    permission="reader",
                    created_by_user_id="user_admin",
                ),
            ]
        )
        db.flush()
        ensure_agent_private_knowledge_branch(
            db,
            "tenant_demo",
            agent.id,
            dedicated_base,
        )
        db.commit()
        dedicated_version = db.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.knowledge_base_id == dedicated_base.id
            )
        ).one()
        dedicated_base_id = dedicated_base.id
        dedicated_version_id = dedicated_version.id
        shared_base_id = knowledge_base.id
        version_1_id = version_1.id
        version_2_id = version_2.id

        first = CapabilityManifestBuilder(db).build(
            "tenant_demo",
            agent.id,
            None,
            None,
            team_id=team.id,
        )
        first_search = next(item for item in first.available if item.name == "knowledge_search")
        first_version_list = next(
            item for item in first.available if item.name == "knowledge_list_versions"
        )
        frozen_versions = dict(first_search.metadata["knowledge_version_by_base_id"])

        knowledge_base.published_version_id = version_2.id
        db.add(knowledge_base)
        db.commit()

        frozen = CapabilityManifestBuilder(db).build(
            "tenant_demo",
            agent.id,
            None,
            None,
            team_id=team.id,
            frozen_knowledge_versions=frozen_versions,
        )
        fresh = CapabilityManifestBuilder(db).build(
            "tenant_demo",
            agent.id,
            None,
            None,
            team_id=team.id,
        )
        frozen_search = next(
            item for item in frozen.available if item.name == "knowledge_search"
        )
        fresh_search = next(item for item in fresh.available if item.name == "knowledge_search")

    assert frozen_versions == {
        dedicated_base_id: dedicated_version_id,
        shared_base_id: version_1_id,
    }
    assert first_version_list.metadata["allowed_knowledge_base_ids"] == [
        shared_base_id
    ]
    assert frozen_search.metadata["knowledge_version_by_base_id"] == {
        dedicated_base_id: dedicated_version_id,
        shared_base_id: version_1_id,
    }
    assert fresh_search.metadata["knowledge_version_by_base_id"] == {
        dedicated_base_id: dedicated_version_id,
        shared_base_id: version_2_id,
    }


def _seed_shared_capability_matrix(
    db: Session,
) -> tuple[Team, KnowledgeBase, KnowledgeBaseVersion, dict[str, str]]:
    """建立读者、编辑者、发布者共用一个共享库的能力授权矩阵。"""
    team = Team(
        id="team_capabilities",
        tenant_id="tenant_demo",
        name="能力验证团队",
        owner_user_id="user_admin",
    )
    shared_base = KnowledgeBase(
        id="kb_shared_capabilities",
        tenant_id="tenant_demo",
        name="团队能力知识库",
        mode="shared",
        published_version_id="kbver_shared_capabilities",
    )
    published = KnowledgeBaseVersion(
        id="kbver_shared_capabilities",
        tenant_id="tenant_demo",
        knowledge_base_id=shared_base.id,
        version="1.0.0",
        name=shared_base.name,
        publication_state="released",
    )
    grants = {
        "agent_reader": "reader",
        "agent_editor": "editor",
        "agent_publisher": "publisher",
    }
    db.add(team)
    db.add(shared_base)
    db.add(published)
    db.add(
        TeamKnowledgeBaseBinding(
            tenant_id="tenant_demo",
            team_id=team.id,
            knowledge_base_id=shared_base.id,
            created_by_user_id="user_admin",
        )
    )
    for agent_id, permission in grants.items():
        db.add(AgentProfile(id=agent_id, tenant_id="tenant_demo", name=agent_id))
        db.add(TeamMember(team_id=team.id, agent_id=agent_id))
        db.add(
            TeamKnowledgeBaseGrant(
                tenant_id="tenant_demo",
                team_id=team.id,
                knowledge_base_id=shared_base.id,
                agent_id=agent_id,
                permission=permission,
                created_by_user_id="user_admin",
            )
        )
    db.commit()
    return team, shared_base, published, grants


@pytest.mark.parametrize(
    ("agent_id", "expected_actions"),
    [
        ("agent_reader", {"knowledge_search", "knowledge_list_versions"}),
        (
            "agent_editor",
            {
                "knowledge_search",
                "knowledge_list_versions",
                "knowledge_create_draft",
                "knowledge_update_draft",
            },
        ),
        (
            "agent_publisher",
            {
                "knowledge_search",
                "knowledge_list_versions",
                "knowledge_create_draft",
                "knowledge_update_draft",
                "knowledge_publish_draft",
                "knowledge_reject_draft",
                "knowledge_rollback",
            },
        ),
    ],
)
def test_team_manifest_filters_shared_knowledge_actions_by_live_grant(
    agent_id: str,
    expected_actions: set[str],
) -> None:
    """团队清单只公开当前授权等级包含的共享知识动作与冻结正式版本。"""
    with _test_session() as db:
        team, shared_base, published, _grants = _seed_shared_capability_matrix(db)

        manifest = CapabilityManifestBuilder(db).build(
            "tenant_demo",
            agent_id,
            None,
            None,
            team_id=team.id,
        )

    actions = {
        item.name: item
        for item in manifest.available
        if item.kind == "knowledge"
    }
    assert set(actions) == expected_actions
    assert all(
        item.metadata["allowed_knowledge_base_ids"] == [shared_base.id]
        for item in actions.values()
    )
    assert all(
        item.metadata["knowledge_version_by_base_id"]
        == {shared_base.id: published.id}
        for item in actions.values()
    )


def test_private_manifest_never_exposes_shared_knowledge_maintenance_actions() -> None:
    """员工私聊即使在团队中拥有发布权，也只能检索自己的专用知识。"""
    with _test_session() as db:
        _team, shared_base, _published, _grants = _seed_shared_capability_matrix(db)
        shared_base_id = shared_base.id
        dedicated_base = KnowledgeBase(
            id="kb_dedicated_publisher",
            tenant_id="tenant_demo",
            name="发布员工专用知识",
            mode="dedicated",
        )
        db.add(dedicated_base)
        db.flush()
        dedicated_base_id = dedicated_base.id
        ensure_agent_private_knowledge_branch(
            db,
            "tenant_demo",
            "agent_publisher",
            dedicated_base,
        )
        db.commit()

        manifest = CapabilityManifestBuilder(db).build(
            "tenant_demo",
            "agent_publisher",
            None,
            None,
        )

    knowledge_actions = {
        item.name: item
        for item in manifest.available
        if item.kind == "knowledge"
    }
    assert set(knowledge_actions) == {"knowledge_search"}
    assert knowledge_actions["knowledge_search"].metadata[
        "allowed_knowledge_base_ids"
    ] == [dedicated_base_id]
    assert shared_base_id not in knowledge_actions["knowledge_search"].metadata[
        "allowed_knowledge_base_ids"
    ]


def test_create_update_and_read_apis_round_trip_capability_scope() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall",
                tenant_id="tenant_demo",
                name="整体智能体",
                is_overall=True,
            )
        )
        db.add(
            AgentProfile(
                id="agent_private",
                tenant_id="tenant_demo",
                name="客服员工",
                is_overall=False,
            )
        )
        db.commit()
        admin = _admin_user()

        general_skill = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                slug="sop-helper",
                name="SOP Helper",
                markdown="# SOP Helper",
                capability_scope="sop_specific",
            ),
            db,
            admin,
        )
        assert general_skill.capability_scope == "sop_specific"
        preserved_general_skill = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                slug="sop-helper",
                original_slug="sop-helper",
                name="SOP Helper",
                markdown="# SOP Helper v2",
            ),
            db,
            admin,
        )
        assert preserved_general_skill.capability_scope == "sop_specific"
        private_general_skill = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id="agent_private",
                slug="sop-helper",
                original_slug="sop-helper",
                name="员工 SOP Helper",
                markdown="# Employee SOP Helper",
            ),
            db,
            admin,
        )
        assert private_general_skill.id != general_skill.id
        assert private_general_skill.capability_scope == "sop_specific"

        tool = create_tool(
            ToolCreateRequest(
                tenant_id="tenant_demo",
                name="sop.lookup",
                url="https://example.test/lookup",
                capability_scope="sop_specific",
            ),
            agent_id="agent_overall",
            db=db,
            current_user=admin,
        )
        assert tool.capability_scope == "sop_specific"
        preserved_tool = update_tool(
            tool.id,
            ToolUpdateRequest(
                tenant_id="tenant_demo",
                name="sop.lookup",
                url="https://example.test/lookup-v2",
            ),
            agent_id="agent_overall",
            db=db,
            current_user=admin,
        )
        assert preserved_tool.capability_scope == "sop_specific"
        ensure_private_resource_binding(
            db,
            "tenant_demo",
            "agent_private",
            "tool",
            tool.id,
            "active",
        )
        db.commit()
        private_tool = update_tool(
            tool.id,
            ToolUpdateRequest(
                tenant_id="tenant_demo",
                name="sop.lookup",
                url="https://example.test/private-lookup",
            ),
            agent_id="agent_private",
            db=db,
            current_user=admin,
        )
        assert private_tool.id != tool.id
        assert private_tool.capability_scope == "sop_specific"

        knowledge_base = create_knowledge_base(
            KnowledgeBaseCreateRequest(
                tenant_id="tenant_demo",
                name="SOP 制度库",
                capability_scope="sop_specific",
            ),
            agent_id="agent_overall",
            db=db,
            current_user=admin,
        )
        assert knowledge_base.capability_scope == "sop_specific"
        updated_knowledge_base = update_knowledge_base(
            knowledge_base.id,
            KnowledgeBaseUpdateRequest(
                tenant_id="tenant_demo",
                capability_scope="general",
            ),
            agent_id="agent_overall",
            db=db,
            current_user=admin,
        )
        assert updated_knowledge_base.capability_scope == "general"


def test_read_models_expose_effective_capability_scope() -> None:
    general_skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="demo",
        name="Demo",
        skill_markdown="# Demo",
        capability_scope="sop_specific",
    )
    tool = Tool(
        tenant_id="tenant_demo",
        name="demo.lookup",
        method="POST",
        url="https://example.test",
        capability_scope="sop_specific",
    )
    knowledge_base = KnowledgeBase(
        tenant_id="tenant_demo",
        name="制度库",
        capability_scope="sop_specific",
    )

    assert general_skill_read(general_skill).capability_scope == "sop_specific"
    assert tool_read(tool).capability_scope == "sop_specific"
    assert knowledge_base_read(knowledge_base, {}).capability_scope == "sop_specific"
    assert normalize_capability_scope("invalid-value") == "general"


def test_knowledge_base_versions_inherit_root_scope() -> None:
    with _test_session() as db:
        knowledge_base = KnowledgeBase(
            tenant_id="tenant_demo",
            name="SOP 制度库",
            capability_scope="sop_specific",
        )
        db.add(knowledge_base)
        db.flush()

        version = ensure_knowledge_base_version(db, knowledge_base)

        assert version.capability_scope == "sop_specific"
        assert knowledge_base_read(
            knowledge_base, {}, version_row=version
        ).capability_scope == "sop_specific"


def test_private_knowledge_scope_update_uses_an_isolated_branch_version() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall",
                tenant_id="tenant_demo",
                name="整体智能体",
                is_overall=True,
            )
        )
        db.add(
            AgentProfile(
                id="agent_private",
                tenant_id="tenant_demo",
                name="客服员工",
                is_overall=False,
            )
        )
        knowledge_base = KnowledgeBase(
            id="kb_shared",
            tenant_id="tenant_demo",
            name="共享制度库",
            capability_scope="general",
        )
        db.add(knowledge_base)
        db.flush()
        base_version = ensure_knowledge_base_version(db, knowledge_base)
        ensure_open_gallery_binding(
            db,
            "tenant_demo",
            "knowledge_base",
            knowledge_base.id,
            "active",
        )
        db.commit()

        private_read = update_knowledge_base(
            knowledge_base.id,
            KnowledgeBaseUpdateRequest(
                tenant_id="tenant_demo",
                capability_scope="sop_specific",
            ),
            agent_id="agent_private",
            db=db,
            current_user=_admin_user(),
        )

        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.agent_id == "agent_private",
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base.id,
            )
        ).one()
        branch_version = db.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.knowledge_base_id == knowledge_base.id,
                KnowledgeBaseVersion.version == branch.head_version,
            )
        ).one()
        db.refresh(knowledge_base)
        db.refresh(base_version)

        assert private_read.capability_scope == "sop_specific"
        assert branch.head_version != branch.base_version
        assert branch_version.capability_scope == "sop_specific"
        assert base_version.capability_scope == "general"
        assert knowledge_base.capability_scope == "general"


def test_mcp_server_scope_cascades_only_to_inherited_children() -> None:
    with _test_session() as db:
        server = MCPServer(
            tenant_id="tenant_demo",
            name="demo",
            capability_scope="general",
        )
        inherited = Tool(
            tenant_id="tenant_demo",
            name="demo.inherited",
            method="POST",
            url="mcp://demo/inherited",
            tool_type="mcp",
            mcp_server_id=server.id,
            capability_scope="general",
            capability_scope_inherited=True,
            config_json={"tool": "inherited"},
        )
        overridden = Tool(
            tenant_id="tenant_demo",
            name="demo.overridden",
            method="POST",
            url="mcp://demo/overridden",
            tool_type="mcp",
            mcp_server_id=server.id,
            capability_scope="general",
            capability_scope_inherited=False,
            config_json={"tool": "overridden"},
        )
        db.add(server)
        db.add(inherited)
        db.add(overridden)
        db.commit()

        server.capability_scope = "sop_specific"
        _update_inherited_mcp_tool_scopes(db, server)
        db.commit()

        rows = {
            row.name: row
            for row in db.exec(select(Tool).where(Tool.mcp_server_id == server.id)).all()
        }
        assert rows["demo.inherited"].capability_scope == "sop_specific"
        assert rows["demo.overridden"].capability_scope == "general"


def test_mcp_sync_inherits_server_scope_and_accepts_child_override() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall",
                tenant_id="tenant_demo",
                name="整体智能体",
                is_overall=True,
            )
        )
        server = MCPServer(
            id="server_builtin",
            tenant_id="tenant_demo",
            name="builtin-demo",
            transport="builtin",
            capability_scope="sop_specific",
        )
        db.add(server)
        db.commit()
        admin = _admin_user()

        sync_mcp_tools(
            server.id,
            MCPSyncRequest(tenant_id="tenant_demo", tool_names=["echo"]),
            db,
            current_user=admin,
        )
        child = db.exec(select(Tool).where(Tool.mcp_server_id == server.id)).one()
        assert child.capability_scope == "sop_specific"
        assert child.capability_scope_inherited is True

        sync_mcp_tools(
            server.id,
            MCPSyncRequest(
                tenant_id="tenant_demo",
                tool_names=["echo"],
                capability_scope_overrides={"echo": "general"},
            ),
            db,
            current_user=admin,
        )
        db.refresh(child)
        assert child.capability_scope == "general"
        assert child.capability_scope_inherited is False


def test_sqlite_capability_scope_migration_backfills_and_is_idempotent(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'capability-scope.db'}")
    table_names = {
        "general_skills",
        "tools",
        "mcp_servers",
        "knowledge_bases",
        "knowledge_base_versions",
    }
    with engine.begin() as conn:
        for table_name in table_names:
            conn.execute(text(f"CREATE TABLE {table_name} (id VARCHAR PRIMARY KEY)"))
            conn.execute(text(f"INSERT INTO {table_name} (id) VALUES ('legacy')"))

    with engine.begin() as conn:
        _migrate_capability_scope_schema(conn, inspect(engine), table_names)

    with engine.begin() as conn:
        for table_name in table_names:
            assert conn.execute(
                text(f"SELECT capability_scope FROM {table_name} WHERE id = 'legacy'")
            ).scalar_one() == "general"
        conn.execute(
            text("UPDATE tools SET capability_scope = 'unsupported' WHERE id = 'legacy'")
        )

    with engine.begin() as conn:
        _migrate_capability_scope_schema(conn, inspect(engine), table_names)
        assert conn.execute(
            text("SELECT capability_scope FROM tools WHERE id = 'legacy'")
        ).scalar_one() == "general"
