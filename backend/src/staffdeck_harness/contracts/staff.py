"""Storage-independent employee composition values used by module sources."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, TYPE_CHECKING
from .security import ResourceRef


@dataclass(frozen=True)
class StaffResourceAssignment:
    id: str
    tenant_id: str
    staff_id: str
    kind: str
    timestamp: datetime
    label: str


@dataclass(frozen=True)
class StaffProfile:
    """Read-only directory entry, not a local ORM row or an execution assembly."""
    id: str
    tenant_id: str
    name: str
    status: str
    ref: ResourceRef
    is_overall: bool = False
    description: str | None = None
    persona_prompt: str | None = None
    harness_max_actions: int = 32
    metadata_json: Mapping[str, Any] = field(default_factory=dict)

if TYPE_CHECKING:
    from staffdeck_harness.composition.slots import SlotDeclaration

@dataclass(frozen=True)
class SessionPolicy:
    max_actions: int = 32
    context_token_budget: int = 32_000
    compaction_trigger_ratio: float = 0.70
    recent_round_limit: int = 6
    step_timeout_seconds: int | None = None
    sandbox_enabled: bool = False
    sandbox_network_mode: str = "all"
    sandbox_allowed_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityBindingView:
    resource_type: str
    resource_id: str
    binding_id: str | None
    ref: ResourceRef
    name: str
    capability_scope: str = "general"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SopView:
    skill_id: str
    row_id: str
    version: str
    name: str
    content: Mapping[str, Any]
    binding_id: str | None
    slot_bindings: Mapping[str, str | Mapping[str, Any]]
    declared_slots: tuple[SlotDeclaration, ...]
    ref: ResourceRef
    capability_scope: str = "general"
    module_bindings: Mapping[str, Any] = field(default_factory=dict)
    capabilities: tuple[CapabilityBindingView, ...] = ()


@dataclass(frozen=True)
class ChannelView:
    binding_id: str
    channel: str
    status: str
    team_id: str | None
    ref: ResourceRef


@dataclass(frozen=True)
class TeamView:
    team_id: str
    role: str
    ref: ResourceRef


@dataclass(frozen=True)
class StaffComposition:
    tenant_id: str
    staff_id: str                   # AgentProfile.id; None-agent turns use the overall agent
    name: str
    is_overall: bool
    status: str
    persona: str | None
    model_route: Mapping[str, str]
    session_policy: SessionPolicy
    capabilities: tuple[CapabilityBindingView, ...]
    sops: tuple[SopView, ...]
    channels: tuple[ChannelView, ...]
    team: TeamView | None
    interactions: tuple[str, ...]
    ref: ResourceRef
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def capability_ids(self, resource_type: str) -> set[str]:
        return {c.resource_id for c in self.capabilities if c.resource_type == resource_type}

    def visible_resource_ids(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for c in self.capabilities:
            out.setdefault(c.resource_type, set()).add(c.resource_id)
        out.setdefault("skill", set()).update(s.skill_id for s in self.sops)
        return out
