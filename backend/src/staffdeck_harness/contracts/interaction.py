"""Pure-data SPI for assignment, notification/forwarding and reply adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True)
class InteractionCall:
    operation: str
    tenant_id: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class InteractionContext:
    config: Mapping[str, Any]
    call_local: Callable[[], Any] = field(repr=False)


class InteractionProvider(Protocol):
    def invoke(self, context: InteractionContext, call: InteractionCall) -> Any: ...
