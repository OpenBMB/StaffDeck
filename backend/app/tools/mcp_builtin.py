from __future__ import annotations

from typing import Any


class BuiltinMCPError(ValueError):
    pass


def execute_builtin_mcp(config: dict[str, Any], arguments: dict[str, Any]) -> Any:
    server = str(config.get("server") or config.get("server_id") or "").strip()
    tool = str(config.get("tool") or config.get("tool_name") or "").strip()
    if server != "builtin.demo":
        raise BuiltinMCPError(f"不支持的内置 MCP server：{server or '<empty>'}")
    if tool == "echo":
        text = str(arguments.get("text") or "")
        return {"text": text, "length": len(text)}
    if tool == "sum":
        numbers = arguments.get("numbers")
        if not isinstance(numbers, list) or not all(
            isinstance(item, (int, float)) and not isinstance(item, bool) for item in numbers
        ):
            raise BuiltinMCPError("sum 工具需要 numbers 数字数组。")
        total = sum(numbers)
        return {"numbers": numbers, "total": total, "count": len(numbers)}
    if tool == "product_lookup":
        product_id = str(arguments.get("product_id") or arguments.get("product_name") or "").strip().lower()
        catalog = {
            "a1": {"product_id": "A1", "display_name": "A1 标准商品", "price": 129.0, "currency": "CNY"},
            "a3": {"product_id": "A3", "display_name": "A3 高阶商品", "price": 239.0, "currency": "CNY"},
        }
        item = catalog.get(product_id)
        return {"found": bool(item), **(item or {"query": product_id})}
    raise BuiltinMCPError(f"不支持的内置 MCP tool：{tool or '<empty>'}")


def builtin_mcp_tool_names() -> list[str]:
    return ["echo", "sum", "product_lookup"]
