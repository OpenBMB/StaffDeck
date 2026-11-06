"""Storage-independent memory provider contract (recall, capture, management)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Protocol


@dataclass(frozen=True)
class MemoryCall:
    operation: Literal["recall", "capture", "list", "clear"]
    tenant_id: str
    user_id: str | None
    agent_id: str | None = None
    session_id: str | None = None
    query: str = ""
    limit: int = 100
    username: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryContext:
    config: Mapping[str, Any]
    call_local: Callable[[], Any] = field(repr=False)


class MemoryProvider(Protocol):
    def invoke(self, context: MemoryContext, call: MemoryCall) -> Any: ...
