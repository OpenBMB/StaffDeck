from __future__ import annotations

from collections.abc import Mapping

from app.harness.contracts import HarnessToolError, JsonValue


class HarnessExecutionError(RuntimeError):
    """Expected Harness failure that is safe to return to the model."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.error = HarnessToolError(
            code=code,
            message=message,
            retryable=retryable,
            details=dict(details or {}),
        )


def harness_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: Mapping[str, JsonValue] | None = None,
) -> HarnessExecutionError:
    return HarnessExecutionError(
        code,
        message,
        retryable=retryable,
        details=details,
    )


__all__ = ["HarnessExecutionError", "harness_error"]
