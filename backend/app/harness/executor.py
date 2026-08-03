from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.harness.contracts import (
    HarnessToolCall,
    HarnessToolContext,
    HarnessToolError,
    HarnessToolResult,
)
from app.harness.errors import HarnessExecutionError
from app.harness.registry import HarnessRegistry


class HarnessExecutor:
    """Validates and executes a single registered tool call."""

    def __init__(self, registry: HarnessRegistry) -> None:
        self.registry = registry

    def execute(
        self,
        context: HarnessToolContext,
        call: HarnessToolCall,
    ) -> HarnessToolResult:
        started = time.monotonic()
        registered = self.registry.get(call.name)
        if registered is None:
            return self._failure(
                call,
                HarnessToolError(
                    code="TOOL_NOT_FOUND",
                    message=f"Harness tool is not registered: {call.name}",
                ),
                started,
            )

        try:
            arguments = registered.argument_model.model_validate(dict(call.arguments))
        except ValidationError as exc:
            errors = [
                {
                    "type": str(item.get("type", "validation_error")),
                    "path": ".".join(str(part) for part in item.get("loc", ())),
                    "message": str(item.get("msg", "Invalid value")),
                }
                for item in exc.errors(include_url=False)
            ]
            return self._failure(
                call,
                HarnessToolError(
                    code="INVALID_ARGUMENTS",
                    message="Tool arguments failed schema validation.",
                    details={"errors": errors},
                ),
                started,
            )

        try:
            raw_data = registered.handler(context, arguments)
            data = self._json_object(raw_data)
            encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > context.limits.max_result_bytes:
                raise HarnessExecutionError(
                    "RESULT_TOO_LARGE",
                    "Tool result exceeds the Harness result-size limit.",
                    details={
                        "actual_bytes": len(encoded),
                        "max_bytes": context.limits.max_result_bytes,
                    },
                )
            return HarnessToolResult(
                call_id=call.call_id,
                tool_name=call.name,
                success=True,
                data=data,
                duration_ms=self._duration_ms(started),
            )
        except HarnessExecutionError as exc:
            return self._failure(call, exc.error, started)
        except (OSError, UnicodeError) as exc:
            return self._failure(
                call,
                HarnessToolError(
                    code="IO_ERROR",
                    message="The workspace operation could not be completed.",
                    retryable=isinstance(exc, (BlockingIOError, TimeoutError)),
                    details={"exception_type": type(exc).__name__},
                ),
                started,
            )
        except Exception as exc:
            return self._failure(
                call,
                HarnessToolError(
                    code="INTERNAL_ERROR",
                    message="The Harness tool failed unexpectedly.",
                    details={"exception_type": type(exc).__name__},
                ),
                started,
            )

    @staticmethod
    def _json_object(value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise HarnessExecutionError(
                "INVALID_TOOL_RESULT",
                "Harness handlers must return a JSON object.",
            )
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise HarnessExecutionError(
                "INVALID_TOOL_RESULT",
                "Harness handler returned a non-JSON value.",
            ) from exc
        return dict(value)

    @staticmethod
    def _failure(
        call: HarnessToolCall,
        error: HarnessToolError,
        started: float,
    ) -> HarnessToolResult:
        return HarnessToolResult(
            call_id=call.call_id,
            tool_name=call.name,
            success=False,
            error=error,
            duration_ms=HarnessExecutor._duration_ms(started),
        )

    @staticmethod
    def _duration_ms(started: float) -> int:
        return max(0, round((time.monotonic() - started) * 1000))


__all__ = ["HarnessExecutor"]
