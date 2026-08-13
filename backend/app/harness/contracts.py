from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, TypeAlias

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = Mapping[str, JsonValue]


@dataclass(frozen=True)
class HarnessLimits:
    """Resource limits enforced for one isolated Harness workspace."""

    max_read_bytes: int = 1024 * 1024
    max_file_bytes: int = 10 * 1024 * 1024
    max_workspace_bytes: int = 100 * 1024 * 1024
    max_entries: int = 1000
    max_result_bytes: int = 200 * 1024

    def __post_init__(self) -> None:
        for name, value in (
            ("max_read_bytes", self.max_read_bytes),
            ("max_file_bytes", self.max_file_bytes),
            ("max_workspace_bytes", self.max_workspace_bytes),
            ("max_entries", self.max_entries),
            ("max_result_bytes", self.max_result_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_file_bytes > self.max_workspace_bytes:
            raise ValueError("max_file_bytes cannot exceed max_workspace_bytes")


@dataclass(frozen=True)
class HarnessToolContext:
    """Trusted execution context.

    ``workspace_root`` must be provisioned by the caller for a single TaskFrame.
    Model-provided paths are always resolved relative to this root.
    """

    run_id: str
    workspace_root: Path
    task_frame_id: str | None = None
    tenant_id: str | None = None
    limits: HarnessLimits = field(default_factory=HarnessLimits)
    # Internal/direct callers preserve the fail-closed historical behavior.
    # Tenant runtime code always passes the administrator's explicit setting.
    sandbox_enabled: bool = True
    sandbox_network_mode: Literal["all", "allowlist", "deny"] = "all"
    sandbox_allowed_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        root = Path(self.workspace_root)
        if not root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        object.__setattr__(self, "workspace_root", root)


@dataclass(frozen=True)
class HarnessToolCall:
    call_id: str
    name: str
    arguments: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ValueError("call_id must be a non-empty string")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string")


@dataclass(frozen=True)
class HarnessToolError:
    code: str
    message: str
    retryable: bool = False
    details: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessToolResult:
    call_id: str
    tool_name: str
    success: bool
    data: JsonObject | None = None
    error: HarnessToolError | None = None
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.success == (self.error is not None):
            raise ValueError("successful results cannot have errors and failed results require one")


@dataclass(frozen=True)
class HarnessToolSpec:
    name: str
    description: str
    input_schema: JsonObject
    side_effect: Literal["read", "write", "delete"] = "read"

    def as_model_tool(self) -> dict[str, Any]:
        """Return the common function-tool shape accepted by chat model adapters."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.input_schema),
            },
        }


__all__ = [
    "HarnessLimits",
    "HarnessToolCall",
    "HarnessToolContext",
    "HarnessToolError",
    "HarnessToolResult",
    "HarnessToolSpec",
    "JsonObject",
    "JsonValue",
]
