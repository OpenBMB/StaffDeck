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
    assert result["month"].endswith("-09") or result["month"].endswith("-10")
