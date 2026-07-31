from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import (
    ensure_knowledge_base_version,
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
)
from app.api.general_skills import general_skill_read, import_general_skill
from app.api.knowledge_bases import (
    create_knowledge_base,
    knowledge_base_read,
    update_knowledge_base,
)
from app.api.tools import (
    _update_inherited_mcp_tool_scopes,
    create_tool,
    sync_mcp_tools,
    tool_read,
    update_tool,
)
from app.capability_scope import normalize_capability_scope
from app.db.database import _migrate_capability_scope_schema
from app.db.models import (
    AgentProfile,
    AgentKnowledgeBranch,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBaseVersion,
    MCPServer,
    Tenant,
    Tool,
    User,
)
from app.general_skills.schema import GeneralSkillImportRequest
from app.knowledge.schema import KnowledgeBaseCreateRequest, KnowledgeBaseUpdateRequest
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
