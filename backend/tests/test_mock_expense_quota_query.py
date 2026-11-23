from datetime import UTC, datetime

from app.api.mock import (
    MockExpenseQuotaQueryRequest,
    mock_expense_quota_query,
)


def test_expense_quota_query_returns_found_employee() -> None:
    result = mock_expense_quota_query(
        MockExpenseQuotaQueryRequest(employee_id="E1001", month="2026-09")
    )

    assert result["found"] is True
    assert result["employee_name"] == "李明"
    assert result["department"] == "销售部"
    assert result["month"] == "2026-09"
    assert result["total_quota"] == 20000.0
    assert result["used"] == 6350.0
    assert result["remaining"] == 13650.0


def test_expense_quota_query_accepts_bare_numeric_employee_id() -> None:
    result = mock_expense_quota_query(
        MockExpenseQuotaQueryRequest(employee_id="1001", month="2026-09")
    )

    assert result["found"] is True
    assert result["employee_id"] == "E1001"


def test_expense_quota_query_returns_miss_for_unknown_employee() -> None:
    result = mock_expense_quota_query(
        MockExpenseQuotaQueryRequest(employee_id="999999", month="2026-09")
    )

    assert result["found"] is False
    assert result["miss_reason"] == "employee_not_found"
    assert "total_quota" not in result
    assert "remaining" not in result
    assert "999999" in result["message"]


def test_expense_quota_query_returns_miss_when_employee_id_missing() -> None:
    result = mock_expense_quota_query(
        MockExpenseQuotaQueryRequest(employee_id="  ", month="2026-09")
    )

    assert result["found"] is False
    assert result["miss_reason"] == "employee_id_required"
    assert "total_quota" not in result


def test_expense_quota_query_defaults_month_to_current() -> None:
    result = mock_expense_quota_query(MockExpenseQuotaQueryRequest(employee_id="E1002"))

    assert result["found"] is True
    assert result["month"] == datetime.now(UTC).strftime("%Y-%m")


def test_legacy_seeded_expense_tool_uses_local_mock_without_mutating_configuration(monkeypatch):
    from types import SimpleNamespace

    import httpx
    from app.db.models import Tool
    from app.tools.tool_executor import ToolExecutor

    stored = "http://58.57.119.30:52008/api/mock/expense/quota_query"
    local = "http://127.0.0.1:5173/api/mock/expense/quota_query"
    responses = iter([local, stored])

    def record_request(request):
        expected = next(responses)
        assert str(request.url) == expected
        return httpx.Response(200, json={"found": False})

    http_client = httpx.Client
    monkeypatch.setattr(
        "app.tools.tool_executor.httpx.Client",
        lambda **kw: http_client(transport=httpx.MockTransport(record_request)),
    )
    executor = object.__new__(ToolExecutor)
    executor.settings = SimpleNamespace(normalized_tool_base_url="http://127.0.0.1:5173", tool_timeout_seconds=5)
    tool = Tool(id="tool_ffcb6ab0306f412d", tenant_id="tenant_demo", name="expense.quota_query",
                method="POST", url=stored)

    assert executor.execute_sync_http(tool, {"employee_id": "unknown"}).success
    assert tool.url == stored
    tool.id = "customer_config"
    assert executor.execute_sync_http(tool, {"employee_id": "unknown"}).success
