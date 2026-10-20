from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import database
from app.db.models import AgentProfile, AgentResourceBinding, Tenant, Tool


def test_tool_binding_semantics_migration_backfills_and_disables_legacy_tool(
    monkeypatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)

    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall",
                tenant_id="tenant_demo",
                name="开放广场",
                is_overall=True,
            )
        )
        db.add(
            AgentProfile(
                id="agent_branch",
                tenant_id="tenant_demo",
                name="销售员工",
                metadata_json={
                    "owner_user_id": "user_owner",
                    "owner_username": "owner",
                    "owner_display_name": "Owner",
                },
            )
        )
        tool = Tool(
            id="tool_legacy",
            tenant_id="tenant_demo",
            name="legacy.tool",
            method="POST",
            url="https://example.test/legacy",
            enabled=False,
        )
        db.add(tool)
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id="agent_branch",
                resource_type="tool",
                resource_id=tool.id,
                status="active",
                metadata_json={"created_from_agent": True},
            )
        )
        db.commit()

    database._migrate_tool_binding_semantics()  # noqa: SLF001

    with Session(engine) as db:
        from sqlmodel import select

        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == "tool_legacy",
            )
        ).one()
        tool = db.get(Tool, "tool_legacy")
        assert tool is not None and tool.enabled is True
        assert binding.status == "inactive"
        assert binding.metadata_json["owner_agent_id"] == "agent_branch"
        assert binding.metadata_json["scope"] == "agent_private"
        assert binding.metadata_json["owner_user_id"] == "user_owner"
        assert binding.metadata_json["owner_username"] == "owner"
