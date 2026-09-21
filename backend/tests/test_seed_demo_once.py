"""demo 预置内容只做一次性初始化（删掉后重启不复活）的回归测试。"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import seed as seed_module
from app.db.models import AgentProfile, GeneralSkill, MCPServer, Skill, Tool
from app.db.seed import (
    DEMO_SEED_MARKER_ID,
    DEMO_SKILL_CONTENTS,
    DEMO_TOOLS,
    MCP_SERVER_TOOLS,
    MCP_SERVERS,
    seed_demo_data,
)

DEMO_SKILL_ID = str(DEMO_SKILL_CONTENTS[0]["skill_id"])
DEMO_TOOL_NAME = str(DEMO_TOOLS[0]["name"])
MCP_SERVER_NAME = str(MCP_SERVERS[0]["name"])


def _fresh_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _marker_count(db: Session) -> int:
    return len(
        db.execute(
            text("SELECT id FROM app_data_migrations WHERE id = :id"),
            {"id": DEMO_SEED_MARKER_ID},
        ).all()
    )


def _drop_marker(db: Session) -> None:
    """模拟标记机制上线前的存量库：预置数据在，但没有标记。"""
    db.execute(
        text("DELETE FROM app_data_migrations WHERE id = :id"),
        {"id": DEMO_SEED_MARKER_ID},
    )
    db.commit()


def _delete_demo_skill(db: Session, skill_id: str) -> None:
    skill = db.exec(select(Skill).where(Skill.skill_id == skill_id)).one()
    db.delete(skill)


def _delete_demo_tool(db: Session, name: str) -> None:
    tool = db.exec(select(Tool).where(Tool.name == name)).one()
    db.delete(tool)


def _delete_mcp_server_with_tools(db: Session, name: str) -> None:
    server = db.exec(select(MCPServer).where(MCPServer.name == name)).one()
    for leaf in (tool["leaf"] for tool in MCP_SERVER_TOOLS.get(name, [])):
        db.delete(db.exec(select(Tool).where(Tool.name == f"{name}.{leaf}")).one())
    db.delete(server)


def test_fresh_library_seeds_demo_content_and_writes_marker() -> None:
    with _fresh_session() as db:
        seed_demo_data(db)
        db.commit()

        assert db.exec(select(Skill).where(Skill.skill_id == DEMO_SKILL_ID)).first() is not None
        assert db.exec(select(Tool).where(Tool.name == DEMO_TOOL_NAME)).first() is not None
        assert db.exec(select(MCPServer).where(MCPServer.name == MCP_SERVER_NAME)).first() is not None
        assert _marker_count(db) == 1


def test_demo_seed_runs_only_once_across_restarts(monkeypatch) -> None:
    calls: list[str] = []
    original = seed_module._seed_demo_skills

    def spy(session: Session) -> None:
        calls.append("skills")
        original(session)

    monkeypatch.setattr(seed_module, "_seed_demo_skills", spy)

    with _fresh_session() as db:
        seed_demo_data(db)
        db.commit()
        seed_demo_data(db)
        db.commit()

    assert calls == ["skills"]


def test_deleted_demo_skill_is_not_resurrected() -> None:
    with _fresh_session() as db:
        seed_demo_data(db)
        db.commit()

        _delete_demo_skill(db, DEMO_SKILL_ID)
        db.commit()
        assert db.exec(select(Skill).where(Skill.skill_id == DEMO_SKILL_ID)).first() is None

        seed_demo_data(db)
        db.commit()

        assert db.exec(select(Skill).where(Skill.skill_id == DEMO_SKILL_ID)).first() is None


def test_deleted_demo_tool_is_not_resurrected() -> None:
    with _fresh_session() as db:
        seed_demo_data(db)
        db.commit()

        _delete_demo_tool(db, DEMO_TOOL_NAME)
        db.commit()

        seed_demo_data(db)
        db.commit()

        assert db.exec(select(Tool).where(Tool.name == DEMO_TOOL_NAME)).first() is None


def test_deleted_mcp_server_and_tools_are_not_resurrected() -> None:
    with _fresh_session() as db:
        seed_demo_data(db)
        db.commit()

        _delete_mcp_server_with_tools(db, MCP_SERVER_NAME)
        db.commit()

        seed_demo_data(db)
        db.commit()

        assert db.exec(select(MCPServer).where(MCPServer.name == MCP_SERVER_NAME)).first() is None
        for leaf in (tool["leaf"] for tool in MCP_SERVER_TOOLS.get(MCP_SERVER_NAME, [])):
            assert db.exec(select(Tool).where(Tool.name == f"{MCP_SERVER_NAME}.{leaf}")).first() is None


def test_existing_library_without_marker_only_gets_marker() -> None:
    """存量库：没有标记时只补标记，不把管理员删掉的资源建回来。"""
    with _fresh_session() as db:
        seed_demo_data(db)
        db.commit()

        _drop_marker(db)
        _delete_demo_tool(db, DEMO_TOOL_NAME)
        db.commit()

        seed_demo_data(db)
        db.commit()

        assert db.exec(select(Tool).where(Tool.name == DEMO_TOOL_NAME)).first() is None
        assert _marker_count(db) == 1


def test_existing_library_keeps_deletions_even_when_all_demo_resources_are_gone() -> None:
    """存量库把 demo 资源全删光时，也不能因为「查不到预置行」而重播。"""
    with _fresh_session() as db:
        seed_demo_data(db)
        db.commit()

        _drop_marker(db)
        for content in DEMO_SKILL_CONTENTS:
            _delete_demo_skill(db, str(content["skill_id"]))
        for config in DEMO_TOOLS:
            _delete_demo_tool(db, str(config["name"]))
        for config in MCP_SERVERS:
            _delete_mcp_server_with_tools(db, str(config["name"]))
        db.commit()

        seed_demo_data(db)
        db.commit()

        for content in DEMO_SKILL_CONTENTS:
            assert (
                db.exec(select(Skill).where(Skill.skill_id == str(content["skill_id"]))).first()
                is None
            )
        for config in DEMO_TOOLS:
            assert db.exec(select(Tool).where(Tool.name == str(config["name"]))).first() is None
        assert _marker_count(db) == 1


def test_existing_library_without_demo_content_still_seeds() -> None:
    """只有基础设施（租户 / 用户）而没有资源时，仍应正常初始化。"""
    with _fresh_session() as db:
        seed_module._ensure_seed_agents(db)
        db.commit()
        assert db.exec(select(GeneralSkill)).first() is None

        seed_demo_data(db)
        db.commit()

        assert db.exec(select(Tool).where(Tool.name == DEMO_TOOL_NAME)).first() is not None
        assert _marker_count(db) == 1


def test_legacy_default_agent_is_still_archived_after_initialization() -> None:
    """预置内容只初始化一次后，系统智能体归一化仍要每次启动都跑。"""
    with _fresh_session() as db:
        seed_demo_data(db)
        db.commit()

        db.add(
            AgentProfile(
                id="agent_tenant_demo_default",
                tenant_id="tenant_demo",
                name="默认智能体",
                description="默认对话可见域",
                status="active",
            )
        )
        db.commit()

        seed_demo_data(db)
        db.commit()

        agent = db.get(AgentProfile, "agent_tenant_demo_default")
        assert agent is not None
        assert agent.status == "archived"
        assert agent.metadata_json["hidden_from_staffdeck"] is True
