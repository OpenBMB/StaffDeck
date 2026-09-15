"""Public source SPI. No database, bearer token, AgentLoop or Host crosses it.

Sources are trusted deployment adapters, not model-selected tools. References
are resolved only within the frozen employee/step binding set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .security import ResourceRef, SecurityContext
from .staff import StaffComposition, SopView
from .runtime_services import ActorIdentity, ExecutionIdentity


@dataclass(frozen=True)
class SourceContext:
    tenant_id: str
    staff_id: str | None
    session_id: str | None = None
    channel: str = "web"
    user_id: str | None = None
    execution: ExecutionIdentity | None = None
    subject: ActorIdentity | None = None
    resource_config: Mapping[str, Any] = field(default_factory=dict)


class StaffSourcePort(Protocol):
    def reference(self, context: SourceContext) -> ResourceRef: ...
    def resolve(self, context: SourceContext) -> StaffComposition: ...
    def model(self, context: SourceContext, model_id: str | None, role: str = "default") -> Any: ...


class SopDefinitionPort(Protocol):
    def resolve(self, context: SourceContext, staff: StaffComposition) -> tuple[SopView, ...]: ...
    def reference(self, context: SourceContext, sop_id: str) -> ResourceRef: ...


class IdentitySourcePort(Protocol):
    def resolve(self, context: SourceContext, identity: Any) -> SecurityContext: ...


@dataclass(frozen=True)
class ResourceDescriptor:
    ref: ResourceRef
    name: str
    operation: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    digest: str | None = None
    available: bool = True
    side_effecting: bool = False
    replayable: bool = True
    idempotency_key_fields: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Additional authorization requirements, e.g. an MCP server owning a tool.
    related_resources: tuple[ResourceRef, ...] = ()
    # Private execution material from this fresh catalog lookup, never copied to
    # public metadata, model schemas, snapshots or persistent invocation payloads.
    prepared_http_definition: Any = field(default=None, repr=False, compare=False)


class ResourceCatalogPort(Protocol):
    def resolve(self, context: SourceContext, resource_type: str,
                resource_id: str, operation: str) -> ResourceDescriptor: ...
