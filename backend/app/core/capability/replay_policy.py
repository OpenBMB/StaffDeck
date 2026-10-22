from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.tools.tool_schema import ToolCall, ToolResult

TOOL_CALL_HISTORY_SLOT = "_tool_call_history"
TOOL_RESULTS_SLOT = "_tool_results"
IDEMPOTENT_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class ToolReplayPolicy:
    @staticmethod
    def signature(tool_name: str, arguments: dict[str, Any]) -> str:
        return json.dumps(
            {"tool_name": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    @classmethod
    def record_result(
        cls,
        slots: dict[str, Any],
        tool_call: ToolCall,
        tool_result: ToolResult,
        *,
        history_reader: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
        call_signature: Callable[[ToolCall], str] | None = None,
        history_signature: Callable[[dict[str, Any]], str] | None = None,
    ) -> dict[str, Any]:
        updated = dict(slots)
        history = (
            history_reader(updated)
            if history_reader
            else cls.history(updated)
        )
        signature = (
            call_signature(tool_call)
            if call_signature
            else cls.signature(tool_call.name, tool_call.arguments)
        )
        signature_for_history = history_signature or cls.history_item_signature
        history_signatures = {signature_for_history(item) for item in history}
        if signature not in history_signatures:
            history.append({"tool_name": tool_call.name, "arguments": tool_call.arguments})

        raw_results = updated.get(TOOL_RESULTS_SLOT)
        results = list(raw_results) if isinstance(raw_results, list) else []
        results.append(
            {
                "tool_name": tool_call.name,
                "arguments": tool_call.arguments,
                "success": tool_result.success,
                "data": tool_result.data,
                "error": tool_result.error.model_dump() if tool_result.error else None,
            }
        )
        updated[TOOL_CALL_HISTORY_SLOT] = history
        updated[TOOL_RESULTS_SLOT] = results
        return updated

    @staticmethod
    def history(slots: dict[str, Any]) -> list[dict[str, Any]]:
        raw_history = slots.get(TOOL_CALL_HISTORY_SLOT)
        if not isinstance(raw_history, list):
            return []
        return [item for item in raw_history if isinstance(item, dict)]

    @classmethod
    def history_item_signature(cls, item: dict[str, Any]) -> str:
        return cls.signature(
            str(item.get("tool_name") or ""),
            item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
        )

    @classmethod
    def configuration(
        cls,
        raw_config: dict[str, Any],
        raw_schema: dict[str, Any],
        *,
        enabled_parser: Callable[[object], bool | None] | None = None,
    ) -> tuple[bool | None, list[str] | None]:
        parse_enabled = enabled_parser or cls.enabled_value
        config = raw_config.get("idempotency", raw_config.get("idempotency_policy"))
        if config is None:
            config = raw_schema.get("x-idempotency", raw_schema.get("x_idempotency"))
        enabled: bool | None = None
        key_fields: list[str] | None = None
        if isinstance(config, dict):
            enabled = parse_enabled(
                config.get("enabled", config.get("mode", config.get("scope")))
            )
            fields = (
                config.get("key_fields")
                or config.get("fields")
                or config.get("argument_fields")
            )
            if isinstance(fields, list):
                key_fields = [str(item).strip() for item in fields if str(item).strip()]
        else:
            enabled = parse_enabled(config)

        requires_confirmation = raw_config.get(
            "requires_confirmation", raw_schema.get("requires_confirmation")
        )
        if enabled is None and parse_enabled(requires_confirmation) is True:
            enabled = True
        return enabled, key_fields

    @staticmethod
    def enabled_value(value: object) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {
            "1",
            "true",
            "yes",
            "on",
            "enabled",
            "enable",
            "replay",
            "session",
            "session_arguments",
        }:
            return True
        if normalized in {
            "0",
            "false",
            "no",
            "off",
            "disabled",
            "disable",
            "none",
            "read_only",
            "readonly",
        }:
            return False
        return None

    @staticmethod
    def arguments(
        arguments: dict[str, Any], key_fields: list[str] | None
    ) -> dict[str, Any]:
        if not key_fields:
            return arguments
        return {field: arguments.get(field) for field in key_fields if field in arguments}

    @staticmethod
    def default_replay_enabled(method: str) -> bool:
        return method.upper() in IDEMPOTENT_WRITE_METHODS
