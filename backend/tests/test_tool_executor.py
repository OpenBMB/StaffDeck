import sys
from pathlib import Path

import httpx

from app.agents.branching import ensure_private_resource_binding
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall
from app.db.models import AgentProfile, MCPServer, Tenant, Tool
from app.security.internal_service import INTERNAL_SERVICE_HEADER, internal_service_token
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


def test_internal_mock_request_adds_service_token_only_for_configured_origin() -> None:
    executor = object.__new__(ToolExecutor)
    executor.settings = type(
        "Settings",
        (),
        {"normalized_tool_base_url": "http://127.0.0.1:5173"},
    )()

    internal = executor._request_headers(
        "http://127.0.0.1:5173/api/mock/order/query",
        {"Content-Type": "application/json"},
    )
    external = executor._request_headers(
        "https://example.test/api/mock/order/query",
        {"Content-Type": "application/json"},
    )

    assert internal[INTERNAL_SERVICE_HEADER] == internal_service_token()
    assert INTERNAL_SERVICE_HEADER not in external


def test_execute_rejects_tool_not_bound_to_current_employee() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        owner = AgentProfile(id="agent_owner", tenant_id="tenant_demo", name="员工 A")
        other = AgentProfile(id="agent_other", tenant_id="tenant_demo", name="员工 B")
        tool = Tool(
            id="tool_private",
            tenant_id="tenant_demo",
            name="private.lookup",
            method="POST",
            url="https://example.test/private",
            enabled=True,
        )
        db.add(owner)
        db.add(other)
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(db, "tenant_demo", owner.id, "tool", tool.id, "active")
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name=tool.name, arguments={}),
            agent_id=other.id,
        )

        assert result.success is False
        assert result.error is not None
        assert result.error.code == "NOT_ALLOWED"


def test_execute_builtin_mcp_tool_success() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            MCPServer(
                id="server_builtin", tenant_id="tenant_demo", name="builtin", transport="builtin"
            )
        )
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.demo_echo",
                display_name="MCP Demo Echo",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/echo",
                mcp_server_id="server_builtin",
                config_json={"tool": "echo"},
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


def test_execution_policy_uses_tool_timeout_and_falls_back_for_invalid_values() -> None:
    executor = object.__new__(ToolExecutor)
    executor.settings = type("Settings", (), {"tool_timeout_seconds": 8.0})()

    configured = Tool(
        tenant_id="tenant_demo",
        name="slow.lookup",
        method="POST",
        url="https://example.test/slow",
        config_json={"execution": {"timeout_seconds": 20}},
    )
    invalid = Tool(
        tenant_id="tenant_demo",
        name="bad.lookup",
        method="POST",
        url="https://example.test/bad",
        config_json={"execution": {"timeout_seconds": 999}},
    )

    assert executor._execution_policy(configured).timeout_seconds == 20
    assert executor._execution_policy(invalid).timeout_seconds == 8


def test_execute_http_tool_passes_configured_timeout_to_client(monkeypatch) -> None:
    captured: dict[str, float] = {}

    class FakeClient:
        def __init__(self, *, timeout: float):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None):
            return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="slow.lookup",
                method="POST",
                url="https://example.test/slow",
                config_json={"execution": {"timeout_seconds": 20}},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            "tenant_demo", ToolCall(name="slow.lookup", arguments={})
        )

    assert result.success is True
    assert captured["timeout"] == 20


def test_execute_mcp_tool_passes_configured_timeout(monkeypatch) -> None:
    captured: dict[str, float] = {}

    def fake_execute(config, arguments, *, timeout_seconds, tool_name):
        captured["timeout"] = timeout_seconds
        return {"ok": True}

    monkeypatch.setattr("app.tools.tool_executor.execute_mcp_tool", fake_execute)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            MCPServer(
                id="server_builtin_timeout",
                tenant_id="tenant_demo",
                name="builtin-timeout",
                transport="builtin",
            )
        )
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.timeout.echo",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/echo",
                mcp_server_id="server_builtin_timeout",
                config_json={
                    "tool": "echo",
                    "execution": {"timeout_seconds": 20},
                },
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            "tenant_demo", ToolCall(name="mcp.timeout.echo", arguments={"text": "hi"})
        )

    assert result.success is True
    assert captured["timeout"] == 20


def test_execute_builtin_mcp_tool_unknown_config_returns_error() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            MCPServer(
                id="server_builtin", tenant_id="tenant_demo", name="builtin", transport="builtin"
            )
        )
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.bad",
                display_name="Bad MCP",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/missing",
                mcp_server_id="server_builtin",
                config_json={"tool": "missing"},
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


def test_execute_stdio_mcp_tool_success() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            MCPServer(
                id="server_stdio",
                tenant_id="tenant_demo",
                name="stdio",
                transport="stdio",
                command=sys.executable,
                args_json=[str(_mock_mcp_server_path())],
            )
        )
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.real_echo",
                display_name="Real MCP Echo",
                tool_type="mcp",
                method="POST",
                url="mcp://stdio/mock/echo",
                mcp_server_id="server_stdio",
                config_json={"tool": "echo"},
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="mcp.real_echo", arguments={"text": "hello real mcp"}),
        )

        assert result.success is True
        assert result.data == {"text": "hello real mcp", "length": 14}


def test_execute_stdio_mcp_tool_error_is_stable() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            MCPServer(
                id="server_stdio",
                tenant_id="tenant_demo",
                name="stdio",
                transport="stdio",
                command=sys.executable,
                args_json=[str(_mock_mcp_server_path())],
            )
        )
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.real_sum",
                display_name="Real MCP Sum",
                tool_type="mcp",
                method="POST",
                url="mcp://stdio/mock/sum",
                mcp_server_id="server_stdio",
                config_json={"tool": "sum"},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="mcp.real_sum", arguments={"numbers": ["bad"]}),
        )

        assert result.success is False
        assert result.error is not None
        assert result.error.code == "MCP_ERROR"
        assert "numbers" in result.error.message


def test_execute_get_tool_preserves_query_string_when_arguments_empty(monkeypatch) -> None:
    requested: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None):
            requested.update({"method": method, "url": url, "params": params})
            return httpx.Response(
                200,
                json={"current": {"temperature_2m": 27.4}},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="weather.forecast",
                display_name="天气查询",
                method="GET",
                url=(
                    "https://api.open-meteo.com/v1/forecast"
                    "?latitude=39.90&longitude=116.40&current=temperature_2m"
                ),
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="weather.forecast", arguments={}),
        )

    assert result.success is True
    assert result.data == {"current": {"temperature_2m": 27.4}}
    assert requested == {
        "method": "GET",
        "url": (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=39.90&longitude=116.40&current=temperature_2m"
        ),
        "params": None,
    }


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
