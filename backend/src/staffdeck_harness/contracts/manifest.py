"""Module manifests, slots, and bindings.

Terminology follows the architecture document:

- ``ModuleKind``   A/C/T/K: code plugin, content package, trusted service, kernel.
- ``SlotName``     The only legal attachment points (``tenant.staff``, ``staff.sop``, ...).
- ``ModuleManifest``  What a module *is*: identity, contract version, the slots
  it can attach to, the capabilities it exposes, and the hooks it contributes.
- ``SlotBinding``  One attachment of one module into one slot of one parent.

Manifests are frozen; a registry seals them at startup. Nothing here reads the
database — projection modules translate ORM rows into these shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping

JsonObject = Mapping[str, Any]


class ModuleKind(str, Enum):
    CODE = "A"        # provider / adapter implementing a stable SPI
    CONTENT = "C"     # Staff, SOP, Skill packages
    TRUSTED = "T"     # independently deployable, platform-unique semantics
    KERNEL = "K"      # contracts, orchestration, idempotency, security boundary


class SlotName(str, Enum):
    RUNTIME_ENGINE = "runtime.engine"
    RUNTIME_KERNEL = "runtime.kernel"          # informational: coordinator / Harness v3 core (not swappable)
    TENANT_STAFF = "tenant.staff"
    STAFF_SOP = "staff.sop"
    STAFF_CAPABILITY = "staff.capability"
    STAFF_MODEL_ROUTE = "staff.model_route"
    STAFF_TEAM = "staff.team"
    STAFF_INTERACTION = "staff.interaction"
    STAFF_CHANNEL = "staff.channel"
    STAFF_INGRESS = "staff.ingress"
    SOP_SLOT_KNOWLEDGE = "sop.slot.knowledge"
    SOP_SLOT_SKILL = "sop.slot.skill"
    SOP_SLOT_ACTION = "sop.slot.action"
    SOP_SLOT_CONTROL = "sop.slot.control"
    HANDOFF_NOTIFIER = "handoff.notifier"
    HANDOFF_REPLY_ENDPOINT = "handoff.reply_endpoint"
    HANDOFF_ASSIGNMENT = "handoff.assignment"
    KNOWLEDGE_IMPORT_SOURCE = "knowledge.import.source"
    EVENT_OBSERVER = "event.observer"
    SECURITY_PEP = "security.pep"
    RUNTIME_MEMORY = "runtime.memory"            # 记忆召回/写入提供者（默认 = 内置 MemoryService）


# Capability operations a SOP node may declare as a logical dependency. The
# concrete module is chosen when a Staff installs the SOP, never inside the SOP.
CapabilityOperation = Literal[
    "knowledge.search/v1",
    "general_skill.consume/v1",
    "tool.invoke/v1",
    "mcp.invoke/v1",
    "a2a.invoke/v1",
    "sandbox.execute/v1",
    "artifact.publish/v1",
    "handoff.request/v1",
    "sop.execute/v1",
]

SLOT_FOR_OPERATION: dict[str, SlotName] = {
    "knowledge.search/v1": SlotName.SOP_SLOT_KNOWLEDGE,
    "general_skill.consume/v1": SlotName.SOP_SLOT_SKILL,
    "tool.invoke/v1": SlotName.SOP_SLOT_ACTION,
    "mcp.invoke/v1": SlotName.SOP_SLOT_ACTION,
    "a2a.invoke/v1": SlotName.SOP_SLOT_ACTION,
    "sandbox.execute/v1": SlotName.SOP_SLOT_ACTION,
    "artifact.publish/v1": SlotName.SOP_SLOT_ACTION,
    "handoff.request/v1": SlotName.SOP_SLOT_CONTROL,
    "sop.execute/v1": SlotName.SOP_SLOT_CONTROL,
}

HookPoint = Literal["pre_step", "pre_tool", "post_tool", "turn_stopping"]
HOOK_POINTS: tuple[HookPoint, ...] = ("pre_step", "pre_tool", "post_tool", "turn_stopping")


@dataclass(frozen=True)
class HookContribution:
    """A module's declared participation in the fixed hook plan."""

    point: HookPoint
    handler: str                     # dotted name resolved by the host, not by the module
    order: int = 100                 # lower runs first within a point
    depends_on: tuple[str, ...] = () # other module_ids that must run before this one


@dataclass(frozen=True)
class ModuleManifest:
    module_id: str
    name: str
    version: str
    kind: ModuleKind
    contract_version: str
    attaches_to: tuple[SlotName, ...] = ()
    provides_operations: tuple[str, ...] = ()
    requires_operations: tuple[str, ...] = ()
    hooks: tuple[HookContribution, ...] = ()
    policy_actions: tuple[str, ...] = ()   # actions this module's host will PEP-check
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.module_id or not self.version or not self.contract_version:
            raise ValueError("module_id, version and contract_version are required")
        if not isinstance(self.kind, ModuleKind):
            object.__setattr__(self, "kind", ModuleKind(str(self.kind)))
        object.__setattr__(self, "attaches_to", tuple(s if isinstance(s, SlotName) else SlotName(str(s)) for s in self.attaches_to))
        for op in (*self.provides_operations, *self.requires_operations):
            if "/" not in op:
                raise ValueError(f"operation must carry a version suffix (name/vN): {op}")


@dataclass(frozen=True)
class SlotBinding:
    """One module attached to one slot on one parent (Staff, SOP node, Handoff...)."""

    binding_id: str
    slot: SlotName
    parent_id: str
    module_id: str
    module_version: str
    config_revision: str = "default"
    logical_name: str | None = None      # SOP-declared slot name, e.g. ``policy_docs``
    resource_ref: str | None = None      # concrete resource this binding resolves to
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class DurableBindingRef:
    """Serializable identity for a binding; contains no live client."""

    binding_id: str
    slot: str
    module_id: str
    module_version: str
    config_revision: str
    resource_ref: str | None = None
