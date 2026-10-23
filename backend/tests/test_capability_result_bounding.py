"""_bounded_capability_result 的小标量随行行为（场景 C2 依赖）。"""

from __future__ import annotations

from typing import Any

from app.core.engine.step_loop import _bounded_capability_result


def test_chunk_count_preserved_inline() -> None:
    result: dict[str, Any] = {
        "success": True,
        "data": {"chunks": [{"content": "a"}, {"content": "b"}]},
        "chunk_count": 2,
    }
    bounded = _bounded_capability_result("knowledge_search", result)
    assert bounded["chunk_count"] == 2
    assert bounded["success"] is True


def test_chunk_count_preserved_when_truncated() -> None:
    """data 超限被截断后，chunk_count 仍须随行（C2 判「检索到/没检索到」）。"""

    result: dict[str, Any] = {
        "success": True,
        "data": {"chunks": [{"content": "x" * 20_000}]},
        "chunk_count": 1,
    }
    bounded = _bounded_capability_result("knowledge_search", result)
    assert bounded.get("truncated") is True
    assert bounded.get("chunk_count") == 1
    assert "data" not in bounded


def test_chunk_count_absent_for_other_tools() -> None:
    result: dict[str, Any] = {"success": True, "data": {"ok": 1}}
    bounded = _bounded_capability_result("query_balance", result)
    assert "chunk_count" not in bounded
