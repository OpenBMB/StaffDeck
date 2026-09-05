"""Security contracts: the one PepPort every protected host speaks.

Two implementations exist per the architecture and only one is active per
deployment: ``OSS_LOCAL`` and ``BUSINESS_BASE``. Modules never call a PEP;
their *host* does, through ``PepPort`` with an action mapped by the module's
``PolicyActionMapper``.

The vocabulary mirrors the Business trust contract so a Base-backed decision
and a local decision are indistinguishable to callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, Sequence, runtime_checkable

JsonObject = Mapping[str, Any]

TenantRole = Literal["admin", "member", "service"]
PrincipalType = Literal["user", "service", "workload"]
ResourceType = Literal[
    "tenant", "organization", "group", "department", "team", "project",
    "agent", "sop", "skill", "tool", "mcp_server", "knowledge_base", "document",
    "capability", "channel", "handoff", "model_config", "session", "runtime",
]
Action = Literal["view", "use", "create", "edit", "manage", "share", "download", "delete", "execute", "advance", "resume", "receive", "send", "read", "write", "delegate"]
SecurityProfileName = Literal["OSS_LOCAL", "BUSINESS_BASE"]


@dataclass(frozen=True)
class SecurityContext:
    """Who is acting. Built by IdentityPort; never by a module."""

    principal_id: str
    tenant_id: str
    principal_type: PrincipalType = "user"
    tenant_role: TenantRole = "member"
    username: str | None = None
    display_name: str | None = None
    provider: str = "local"           # local | base_identity | channel | workload
    session_id: str | None = None
    token_id: str | None = None
    channel: str | None = None
    workload: JsonObject | None = None   # WorkloadContextPort output when acting as a service
    attributes: JsonObject = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.tenant_role == "admin"


@dataclass(frozen=True)
class ResourceRef:
    type: ResourceType
    id: str
    tenant_id: str
    attributes: JsonObject = field(default_factory=dict)   # owner_user_id, is_overall, published...


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    decision_id: str | None = None
    revision: str | None = None
    source: SecurityProfileName | Literal["local_fence"] = "OSS_LOCAL"
    pending: bool = False   # Business only: authz projection not yet settled

    @classmethod
    def allow(cls, reason: str = "allowed", **kw: Any) -> "Decision":
        return cls(allowed=True, reason=reason, **kw)

    @classmethod
    def deny(cls, reason: str, **kw: Any) -> "Decision":
        return cls(allowed=False, reason=reason, **kw)


@runtime_checkable
class PepPort(Protocol):
    """Policy Enforcement Point. Deny by default; fail closed on errors."""

    profile: SecurityProfileName

    def authorize(self, ctx: SecurityContext, module: str, action: str, resource: ResourceRef) -> Decision: ...

    def filter(
        self, ctx: SecurityContext, module: str, action: str, resources: Sequence[ResourceRef]
    ) -> list[ResourceRef]: ...


@runtime_checkable
class IdentityPort(Protocol):
    """Turns an authenticated subject (User row, gateway JWT, channel identity) into a SecurityContext."""

    def from_user(self, user: Any, *, channel: str | None = None) -> SecurityContext: ...

    def from_service(self, service_id: str, tenant_id: str, *, workload: JsonObject | None = None) -> SecurityContext: ...


@runtime_checkable
class WorkloadContextPort(Protocol):
    """Mints short-lived workload context for asynchronous runtime calls (Business: Base workload JWT)."""

    def mint(self, ctx: SecurityContext, *, audience: str, ttl_seconds: int) -> JsonObject: ...


@dataclass(frozen=True)
class SecurityProfile:
    name: SecurityProfileName
    identity: IdentityPort
    pep: PepPort
    workload: WorkloadContextPort


class PolicyActionMapper:
    """Maps a module operation to (action, resource type). Owned by the module, called by its host."""

    def __init__(self, mapping: Mapping[str, tuple[Action, ResourceType]]):
        self._mapping = dict(mapping)

    def map(self, operation: str) -> tuple[Action, ResourceType]:
        try:
            return self._mapping[operation]
        except KeyError as exc:
            raise KeyError(f"no policy action mapped for operation {operation!r}") from exc


DEFAULT_ACTION_MAP: dict[str, tuple[Action, ResourceType]] = {
    "knowledge.search/v1": ("use", "knowledge_base"),
    "general_skill.consume/v1": ("use", "skill"),
    "tool.invoke/v1": ("use", "tool"),
    "mcp.invoke/v1": ("use", "tool"),
    "a2a.invoke/v1": ("use", "tool"),
    "sandbox.execute/v1": ("execute", "capability"),
    "artifact.publish/v1": ("write", "session"),
    "handoff.request/v1": ("create", "handoff"),
    "sop.execute/v1": ("execute", "sop"),
    "staff.use/v1": ("use", "agent"),
    "staff.manage/v1": ("manage", "agent"),
    "sop.read/v1": ("view", "sop"),
    "sop.advance/v1": ("advance", "sop"),
    "sop.resume/v1": ("resume", "sop"),
    "channel.receive/v1": ("receive", "channel"),
    "channel.send/v1": ("send", "channel"),
    "handoff.assign/v1": ("manage", "handoff"),
    "handoff.reply/v1": ("edit", "handoff"),
    "handoff.notify/v1": ("send", "handoff"),
    "memory.read/v1": ("read", "session"),
    "memory.write/v1": ("write", "session"),
    "team.delegate/v1": ("delegate", "team"),
    "model.use/v1": ("use", "model_config"),
    "runtime.turn/v1": ("execute", "runtime"),
}
