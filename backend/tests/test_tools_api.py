import sys
from pathlib import Path

import pytest
import httpx
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_open_gallery_binding
from app.api.tools import _normalize_probe_url, delete_tool, list_tools, probe_tool
from app.config import get_settings
from app.db.models import AgentProfile, AgentResourceBinding, Tenant, Tool
from app.tools.tool_schema import ToolProbeRequest


def test_delete_tool_removes_tenant_tool() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        tool = Tool(
            tenant_id="tenant_demo",
            name="product.lookup",
            display_name="商品查询",
            method="POST",
            url="/api/mock/product/lookup",
        )
        db.add(tool)
        db.commit()
        db.refresh(tool)

        result = delete_tool(tool.id, "tenant_demo", db)

        assert result == {"status": "deleted"}
        assert db.get(Tool, tool.id) is None


def test_delete_tool_is_tenant_scoped() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(Tenant(id="tenant_other", name="Other"))
        tool = Tool(
            tenant_id="tenant_other",
            name="product.lookup",
            display_name="商品查询",
            method="POST",
            url="/api/mock/product/lookup",
        )
        db.add(tool)
        db.commit()
        db.refresh(tool)

        with pytest.raises(HTTPException) as exc_info:
            delete_tool(tool.id, "tenant_demo", db)

        assert exc_info.value.status_code == 404
        assert db.get(Tool, tool.id) is not None


def test_open_gallery_delete_tool_hides_gallery_without_removing_agent_binding() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True))
        db.add(AgentProfile(id="agent_branch", tenant_id="tenant_demo", name="研发员工", is_overall=False))
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather.forecast",
            display_name="天气查询",
            method="POST",
            url="/api/mock/weather",
        )
        db.add(tool)
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id="agent_branch",
                resource_type="tool",
                resource_id=tool.id,
                status="active",
            )
        )
        db.commit()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id, "active")
        db.commit()

        result = delete_tool(tool.id, "tenant_demo", db, agent_id="agent_overall")

        assert result == {"status": "hidden"}
        assert db.get(Tool, tool.id) is not None
        branch_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == "agent_branch",
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == tool.id,
            )
        ).one()
        assert branch_binding.status == "active"
        assert list_tools("tenant_demo", bucket=None, agent_id="agent_overall", db=db) == []
        assert list_tools("tenant_demo", bucket=None, agent_id="agent_branch", db=db)[0].id == tool.id


def test_open_gallery_tool_read_returns_system_creator_metadata() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True))
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather.forecast",
            display_name="天气查询",
            method="POST",
            url="/api/mock/weather",
        )
        db.add(tool)
        db.commit()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id, "active")
        db.commit()

        rows = list_tools("tenant_demo", bucket=None, agent_id="agent_overall", db=db)

        assert len(rows) == 1
        assert rows[0].metadata["creator_name"] == "admin"
        assert rows[0].metadata["created_by_username"] == "admin"


def test_probe_tool_success_infers_output_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None):
            assert method == "POST"
            assert url == "http://localhost:5173/api/mock/member/benefit-reconcile"
            assert json == {"user_id": "user_demo", "order_id": "A12345"}
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "missing_benefits": [{"benefit_id": "coupon_001", "amount": 30}],
                },
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="member.benefit_reconcile",
                method="POST",
                url="/api/mock/member/benefit-reconcile",
                sample_arguments={"user_id": "user_demo", "order_id": "A12345"},
            ),
            db,
        )

        assert result.success is True
        assert result.status_code == 200
        assert result.inferred_output_schema["properties"]["found"]["type"] == "boolean"
        assert result.inferred_output_schema["properties"]["missing_benefits"]["type"] == "array"


def test_probe_mcp_tool_success_infers_output_schema() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="mcp.demo_sum",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/sum",
                mcp_config={"server": "builtin.demo", "tool": "sum"},
                sample_arguments={"numbers": [1, 2, 3]},
            ),
            db,
        )

        assert result.success is True
        assert result.status_code == 200
        assert result.data_preview == {"numbers": [1, 2, 3], "total": 6, "count": 3}
        assert result.inferred_output_schema["properties"]["total"]["type"] == "integer"


def test_probe_get_tool_preserves_query_string_when_arguments_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            return httpx.Response(200, json={"current": {"temperature_2m": 27.4}})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="weather.forecast",
                method="GET",
                url=(
                    "https://api.open-meteo.com/v1/forecast"
                    "?latitude=39.90&longitude=116.40&current=temperature_2m"
                ),
                sample_arguments={},
            ),
            db,
        )

    assert result.success is True
    assert requested == {
        "method": "GET",
        "url": (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=39.90&longitude=116.40&current=temperature_2m"
        ),
        "params": None,
    }


def test_probe_get_tool_sends_sample_arguments_as_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None):
            requested.update({"method": method, "url": url, "params": params, "json": json})
            return httpx.Response(200, json={"timezone": "Asia/Shanghai"})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="weather.forecast",
                method="GET",
                url="https://api.open-meteo.com/v1/forecast",
                sample_arguments={
                    "latitude": "39.90",
                    "longitude": "116.40",
                    "current": "temperature_2m,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Shanghai",
                },
            ),
            db,
        )

    assert result.success is True
    assert requested == {
        "method": "GET",
        "url": "https://api.open-meteo.com/v1/forecast",
        "params": {
            "latitude": "39.90",
            "longitude": "116.40",
            "current": "temperature_2m,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": "Asia/Shanghai",
        },
        "json": None,
    }


def test_probe_mcp_tool_error_is_stable() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="mcp.bad",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/missing",
                mcp_config={"server": "builtin.demo", "tool": "missing"},
                sample_arguments={},
            ),
            db,
        )

        assert result.success is False
        assert result.status_code == 400
        assert result.error is not None
        assert result.error.code == "MCP_ERROR"


def test_probe_stdio_mcp_tool_success_infers_output_schema() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="mcp.real_product_lookup",
                tool_type="mcp",
                method="POST",
                url="mcp://stdio/mock/product_lookup",
                mcp_config={
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(_mock_mcp_server_path())],
                    "tool": "product_lookup",
                },
                sample_arguments={"product_id": "A1"},
            ),
            db,
        )

        assert result.success is True
        assert result.status_code == 200
        assert result.data_preview["found"] is True
        assert result.data_preview["price"] == 129.0
        assert result.inferred_output_schema["properties"]["price"]["type"] == "number"


def test_probe_tool_relative_url_uses_configured_tool_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOL_BASE_URL", "http://127.0.0.1:10086/")
    get_settings.cache_clear()
    try:
        assert _normalize_probe_url("/api/mock/member/benefit-reconcile") == (
            "http://127.0.0.1:10086/api/mock/member/benefit-reconcile"
        )
    finally:
        get_settings.cache_clear()


def test_probe_tool_http_error_returns_stable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None):
            return httpx.Response(404, json={"detail": "not found"})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="missing.tool",
                method="POST",
                url="http://example.invalid/missing",
                sample_arguments={"query": "x"},
            ),
            db,
        )

        assert result.success is False
        assert result.status_code == 404
        assert result.error is not None
        assert result.error.code == "HTTP_ERROR"


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _mock_mcp_server_path() -> Path:
    return Path(__file__).resolve().parents[1] / "mock_servers" / "mcp_stdio_server.py"
