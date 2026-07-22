import sys
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.tools import (
    create_mcp_server,
    delete_mcp_server,
    discover_mcp_tools,
    discover_mcp_tools_adhoc,
    list_tools,
    sync_mcp_tools,
)
from app.db.models import MCPServer, Tenant, Tool, User
from app.db.models import AgentProfile, AgentResourceBinding
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import (
    MCPDiscoverRequest,
    MCPServerConnection,
    MCPServerCreateRequest,
    MCPSyncRequest,
    ToolCall,
)


def _admin_user() -> User:
    return User(id="user_admin", tenant_id="tenant_demo", username="ops", role="admin", password_hash="test")


def _member_user() -> User:
    return User(id="user_member", tenant_id="tenant_demo", username="member", role="member", password_hash="test")


def test_discover_builtin_mcp_server_lists_tools() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        response = discover_mcp_tools_adhoc(
            MCPDiscoverRequest(
                tenant_id="tenant_demo",
                connection=MCPServerConnection(transport="builtin"),
            ),
            db,
            _member_user(),
        )

        assert response.success is True
        names = {tool.name for tool in response.tools}
        assert {"echo", "sum", "product_lookup"} <= names
        echo = next(tool for tool in response.tools if tool.name == "echo")
        assert echo.input_schema["properties"]["text"]["type"] == "string"


def test_discover_stdio_mcp_server_lists_tools() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        response = discover_mcp_tools_adhoc(
            MCPDiscoverRequest(
                tenant_id="tenant_demo",
                connection=MCPServerConnection(
                    transport="stdio",
                    command=sys.executable,
                    args=[str(_mock_mcp_server_path())],
                ),
            ),
            db,
            _member_user(),
        )

        assert response.success is True
        names = {tool.name for tool in response.tools}
        assert {"echo", "sum", "product_lookup"} <= names


def test_sync_mcp_tools_imports_tools_and_executes() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True))
        db.commit()

        server = create_mcp_server(
            MCPServerCreateRequest(
                tenant_id="tenant_demo",
                name="builtin_demo",
                display_name="内置 Demo MCP",
                connection=MCPServerConnection(transport="builtin"),
            ),
            db,
            _admin_user(),
        )

        sync = sync_mcp_tools(
            server.id,
            MCPSyncRequest(tenant_id="tenant_demo", tool_names=["echo"]),
            db,
            current_user=_admin_user(),
        )

        assert sync.success is True
        assert sync.imported == ["echo"]

        tools = db.exec(select(Tool).where(Tool.mcp_server_id == server.id)).all()
        assert len(tools) == 1
        imported = tools[0]
        assert imported.name == "builtin_demo.echo"
        assert imported.tool_type == "mcp"
        assert imported.config_json == {"tool": "echo"}
        assert imported.input_schema["properties"]["text"]["type"] == "string"
        # display_name 应为工具名（leaf），不能是描述文本（否则列表里名字/描述会叠加）。
        assert imported.display_name == "echo"
        assert imported.description and imported.description != imported.display_name
        # 同步的工具应建立 open gallery 绑定，才能在工具广场列表中可见。
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == imported.id,
            )
        ).first()
        assert binding is not None
        # 端到端：工具广场列表应能查到这个同步进来的工具。
        listed = list_tools(tenant_id="tenant_demo", bucket=None, agent_id="agent_overall", db=db)
        assert any(item.name == "builtin_demo.echo" for item in listed)

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="builtin_demo.echo", arguments={"text": "hi"}),
        )
        assert result.success is True
        assert result.data == {"text": "hi", "length": 2}


def test_sync_mcp_tools_preserves_execution_policy() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        server = MCPServer(
            id="server_builtin_policy",
            tenant_id="tenant_demo",
            name="builtin-policy",
            transport="builtin",
        )
        db.add(server)
        db.add(
            Tool(
                id="tool_policy",
                tenant_id="tenant_demo",
                name="mcp.builtin-policy.echo",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin-policy/echo",
                mcp_server_id=server.id,
                config_json={"tool": "echo", "execution": {"timeout_seconds": 20}},
            )
        )
        db.commit()

        sync_mcp_tools(
            server.id,
            MCPSyncRequest(tenant_id="tenant_demo", tool_names=["echo"]),
            db,
            current_user=_admin_user(),
        )

        tool = db.get(Tool, "tool_policy")
        assert tool is not None
        assert tool.config_json == {"tool": "echo", "execution": {"timeout_seconds": 20}}


def test_sync_mcp_tools_scoped_to_employee_binds_privately() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True))
        db.add(AgentProfile(id="agent_employee", tenant_id="tenant_demo", name="数字员工", is_overall=False))
        db.commit()

        server = create_mcp_server(
            MCPServerCreateRequest(
                tenant_id="tenant_demo",
                name="builtin_demo",
                connection=MCPServerConnection(transport="builtin"),
            ),
            db,
            _admin_user(),
        )

        sync = sync_mcp_tools(
            server.id,
            MCPSyncRequest(tenant_id="tenant_demo", tool_names=["echo"]),
            db,
            agent_id="agent_employee",
            current_user=_admin_user(),
        )
        assert sync.success is True
        assert sync.imported == ["echo"]

        imported = db.exec(select(Tool).where(Tool.mcp_server_id == server.id)).first()
        assert imported is not None

        # 员工范围内同步应建立私有绑定，工具只对该员工可见，不出现在工具广场。
        employee_tools = list_tools(tenant_id="tenant_demo", bucket=None, agent_id="agent_employee", db=db)
        assert any(item.id == imported.id for item in employee_tools)

        plaza_tools = list_tools(tenant_id="tenant_demo", bucket=None, agent_id="agent_overall", db=db)
        assert all(item.id != imported.id for item in plaza_tools)


def test_sync_is_idempotent_and_updates_schema() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        server = create_mcp_server(
            MCPServerCreateRequest(
                tenant_id="tenant_demo",
                name="builtin_demo",
                connection=MCPServerConnection(transport="builtin"),
            ),
            db,
            _admin_user(),
        )

        first = sync_mcp_tools(
            server.id,
            MCPSyncRequest(tenant_id="tenant_demo"),
            db,
            current_user=_admin_user(),
        )
        assert first.success is True
        assert len(first.imported) == 3

        second = sync_mcp_tools(
            server.id,
            MCPSyncRequest(tenant_id="tenant_demo"),
            db,
            current_user=_admin_user(),
        )
        assert second.success is True
        assert second.imported == []
        assert set(second.updated) == {"echo", "sum", "product_lookup"}

        tools = db.exec(select(Tool).where(Tool.mcp_server_id == server.id)).all()
        assert len(tools) == 3


def test_discover_saved_server_marks_imported() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        server = create_mcp_server(
            MCPServerCreateRequest(
                tenant_id="tenant_demo",
                name="builtin_demo",
                connection=MCPServerConnection(transport="builtin"),
            ),
            db,
            _admin_user(),
        )
        sync_mcp_tools(
            server.id,
            MCPSyncRequest(tenant_id="tenant_demo", tool_names=["echo"]),
            db,
            current_user=_admin_user(),
        )

        response = discover_mcp_tools(
            server.id,
            MCPDiscoverRequest(tenant_id="tenant_demo"),
            db,
            _admin_user(),
        )

        assert response.success is True
        by_name = {tool.name: tool for tool in response.tools}
        assert by_name["echo"].imported is True
        assert by_name["echo"].tool_id is not None
        assert by_name["sum"].imported is False


def test_delete_mcp_server_removes_tools() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        server = create_mcp_server(
            MCPServerCreateRequest(
                tenant_id="tenant_demo",
                name="builtin_demo",
                connection=MCPServerConnection(transport="builtin"),
            ),
            db,
            _admin_user(),
        )
        sync_mcp_tools(
            server.id,
            MCPSyncRequest(tenant_id="tenant_demo", tool_names=["echo"]),
            db,
            current_user=_admin_user(),
        )

        result = delete_mcp_server(
            server.id,
            "tenant_demo",
            db,
            agent_id=None,
            remove_tools=True,
            current_user=_admin_user(),
        )

        assert result == {"status": "deleted"}
        assert db.get(MCPServer, server.id) is None
        assert len(db.exec(select(Tool).where(Tool.mcp_server_id == server.id)).all()) == 0


def _mock_mcp_server_path() -> Path:
    return Path(__file__).resolve().parents[1] / "mock_servers" / "mcp_stdio_server.py"


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
