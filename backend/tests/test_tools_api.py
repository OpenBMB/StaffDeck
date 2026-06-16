import pytest
import httpx
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.tools import _normalize_probe_url, delete_tool, probe_tool
from app.config import get_settings
from app.db.models import Tenant, Tool
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
            assert url == "http://localhost:8000/api/mock/member/benefit-reconcile"
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
