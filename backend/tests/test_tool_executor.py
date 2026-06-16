from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall
from app.db.models import Tenant, Tool
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


def test_resolve_secret_header(monkeypatch):
    monkeypatch.setenv("ORDER_API_TOKEN", "token-123")
    executor = object.__new__(ToolExecutor)

    headers = executor._resolve_headers(
        {"Authorization": "Bearer ${secret.ORDER_API_TOKEN}"},
        {},
    )

    assert headers["Authorization"] == "Bearer token-123"


def test_execute_builtin_mcp_tool_success() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.demo_echo",
                display_name="MCP Demo Echo",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/echo",
                config_json={"server": "builtin.demo", "tool": "echo"},
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="mcp.demo_echo", arguments={"text": "hello mcp"}),
        )

        assert result.success is True
        assert result.data == {"text": "hello mcp", "length": 9}


def test_execute_builtin_mcp_tool_unknown_config_returns_error() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.bad",
                display_name="Bad MCP",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/missing",
                config_json={"server": "builtin.demo", "tool": "missing"},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="mcp.bad", arguments={}),
        )

        assert result.success is False
        assert result.error is not None
        assert result.error.code == "MCP_ERROR"


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
