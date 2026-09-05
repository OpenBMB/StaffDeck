"""Public capability SPI. No ORM, Host, model client or AgentLoop crosses this boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .invocation import ModuleInvocation, ModuleResult


@dataclass(frozen=True)
class ProviderContext:
    module_id: str
    module_version: str
    config: Mapping[str, Any]
    resource_ids: tuple[str, ...]
    resource_digests: Mapping[str, str]
    remaining_seconds: Callable[[], float | None]
    emit: Callable[[str, dict[str, Any]], None]
    # Optional platform service for the SAME authorized invocation. The built-in SD
    # adapters use this; a standalone remote provider only needs inv/context/config.
    call_local: Callable[[], ModuleResult] = field(repr=False)


class CapabilityProvider(Protocol):
    def invoke(self, context: ProviderContext, invocation: ModuleInvocation) -> ModuleResult: ...
