"""CompositionCompiler: Staff -> SOP -> Capability -> Hook, frozen into one snapshot.

The snapshot is the single artifact every later phase reads: pre-loop
preparation, in-loop hooks, the Bridge's activation allowlist, and post-loop
reducers. Its ``snapshot_id`` is a sha256 over a canonical JSON projection, so
two turns with identical composition share an id and any binding change
produces a new one (bindings take effect on the next turn — never mid-turn).

Publish-time validation (also run per turn, cheaply):

- ``RequiredSlotMissing``  a required SOP slot has no Staff binding
- ``SlotNotBound``         a slot is bound to a resource the Staff cannot use
- ``ContractIncompatible`` a provider's contract version is not supported
- ``DependencyCycle``      sub-SOP graph is cyclic
- ``HookCycle``            hook ``depends_on`` edges are cyclic
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from staffdeck_harness.composition.slots import ResolvedSlot, resolve_slots
from staffdeck_harness.composition.staff import StaffComposition
from staffdeck_harness.contracts.errors import ContractIncompatible, DependencyCycle, HookCycle
from staffdeck_harness.contracts.manifest import HOOK_POINTS, HookContribution, HookPoint

SUPPORTED_CONTRACTS: dict[str, set[str]] = {
    "knowledge.search": {"v1"},
    "general_skill.consume": {"v1"},
    "tool.invoke": {"v1"},
    "mcp.invoke": {"v1"},
    "a2a.invoke": {"v1"},
    "sandbox.execute": {"v1"},
    "artifact.publish": {"v1"},
    "handoff.request": {"v1"},
    "sop.execute": {"v1"},
}


@dataclass(frozen=True)
class CapabilityGrant:
    """One capability the model may reach in this turn, already resource-resolved."""

    operation: str
    resource_type: str
    resource_id: str
    name: str
    binding_id: str | None = None
    scope: str = "general"           # general | sop_specific
    sop_id: str | None = None
    node_id: str | None = None
    slot_name: str | None = None
    required: bool = False
    # Provider pinning: which module serves this operation for this Staff/SOP. ``None`` means the
    # default (first enabled provider). Pinned at compile time so the snapshot is a frozen contract.
    provider_module_id: str | None = None
    provider_version: str | None = None


@dataclass(frozen=True)
class SopExecutionPlan:
    skill_id: str
    version: str
    name: str
    content: Mapping[str, Any]
    resolved_slots: tuple[ResolvedSlot, ...]
    sub_sop_ids: tuple[str, ...]


@dataclass(frozen=True)
class HookPlan:
    """Ordered handlers per hook point after cycle validation."""

    order: Mapping[HookPoint, tuple[HookContribution, ...]]


@dataclass(frozen=True)
class CompositionSnapshot:
    snapshot_id: str
    tenant_id: str
    staff_id: str
    staff_ref_attributes: Mapping[str, Any]
    persona: str | None
    model_route: Mapping[str, str]
    session_policy: Mapping[str, Any]
    grants: tuple[CapabilityGrant, ...]
    sops: tuple[SopExecutionPlan, ...]
    hooks: HookPlan
    channels: tuple[str, ...]
    team_id: str | None
    interactions: tuple[str, ...]
    generation: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def grants_for(self, *, sop_id: str | None = None, node_id: str | None = None) -> tuple[CapabilityGrant, ...]:
        """General grants plus the grants of the active SOP node (if any)."""

        out = [g for g in self.grants if g.scope == "general"]
        if sop_id:
            out.extend(g for g in self.grants if g.sop_id == sop_id and (node_id is None or g.node_id in (None, node_id)))
        return tuple(out)

    def allowed_resource_ids(self, *, sop_id: str | None = None, node_id: str | None = None) -> dict[str, set[str]]:
        allowed: dict[str, set[str]] = {}
        for g in self.grants_for(sop_id=sop_id, node_id=node_id):
            allowed.setdefault(g.resource_type, set()).add(g.resource_id)
        return allowed

    def sop(self, skill_id: str) -> SopExecutionPlan | None:
        return next((s for s in self.sops if s.skill_id == skill_id), None)


# --------------------------------------------------------------------------- helpers

def _check_contracts(operations: Iterable[str]) -> None:
    for op in operations:
        name, _, version = op.partition("/")
        if name not in SUPPORTED_CONTRACTS or version not in SUPPORTED_CONTRACTS[name]:
            raise ContractIncompatible(f"unsupported capability contract {op!r}", details={"operation": op})


def _detect_cycle(edges: Mapping[str, Iterable[str]], *, error: type[Exception], what: str) -> list[str]:
    """Kahn topological order; raises on cycle."""

    nodes = set(edges) | {t for ts in edges.values() for t in ts}
    indeg = {n: 0 for n in nodes}
    for src, targets in edges.items():
        for t in targets:
            indeg[t] += 1
    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for t in sorted(edges.get(n, ())):
            indeg[t] -= 1
            if indeg[t] == 0:
                ready.append(t)
    if len(order) != len(nodes):
        remaining = sorted(n for n in nodes if n not in order)
        raise error(f"{what} cycle detected among {remaining}", details={"nodes": remaining})
    return order


def _sub_sop_ids(content: Mapping[str, Any]) -> tuple[str, ...]:
    ids: list[str] = []
    for node in content.get("nodes") or content.get("steps") or []:
        if isinstance(node, Mapping):
            sid = node.get("sub_sop_id") or ((node.get("subflow") or {}).get("sop_id") if isinstance(node.get("subflow"), Mapping) else None)
            if sid:
                ids.append(str(sid))
    return tuple(ids)


def compile_hooks(contributions: Sequence[HookContribution]) -> HookPlan:
    order: dict[HookPoint, tuple[HookContribution, ...]] = {}
    for point in HOOK_POINTS:
        items = [c for c in contributions if c.point == point]
        edges: dict[str, list[str]] = {c.handler: [] for c in items}
        by_handler = {c.handler: c for c in items}
        for c in items:
            for dep in c.depends_on:
                if dep in by_handler:
                    edges.setdefault(dep, []).append(c.handler)
        topo = _detect_cycle(edges, error=HookCycle, what=f"hook[{point}]")
        rank = {h: i for i, h in enumerate(topo)}
        order[point] = tuple(sorted(items, key=lambda c: (c.order, rank.get(c.handler, 0), c.handler)))
    return HookPlan(order=order)


DEFAULT_HOOKS: tuple[HookContribution, ...] = (
    HookContribution(point="pre_step", handler="persona", order=10),
    HookContribution(point="pre_step", handler="memory.recall", order=20),
    HookContribution(point="pre_step", handler="sop.execution_slice", order=30, depends_on=("memory.recall",)),
    HookContribution(point="pre_tool", handler="activation.allowlist", order=10),
    HookContribution(point="pre_tool", handler="capability.pep", order=20, depends_on=("activation.allowlist",)),
    HookContribution(point="post_tool", handler="ledger.record", order=10),
    HookContribution(point="post_tool", handler="citations.collect", order=20),
    HookContribution(point="turn_stopping", handler="sop.output_supervisor", order=10),
    HookContribution(point="turn_stopping", handler="handoff.detect", order=20, depends_on=("sop.output_supervisor",)),
)


class CompositionCompiler:
    def __init__(self, *, hooks: Sequence[HookContribution] | None = None, supported_contracts: Mapping[str, set[str]] | None = None):
        if hooks is None:
            try:
                from staffdeck_harness.modules.registry import peek_registry

                reg = peek_registry()
                hooks = (reg.hooks() if reg is not None else ()) or DEFAULT_HOOKS
            except Exception:  # registry unavailable (unit tests without settings) -> shipped defaults
                hooks = DEFAULT_HOOKS
        self.hooks = tuple(hooks)
        if supported_contracts is not None:
            SUPPORTED_CONTRACTS.update(supported_contracts)

    def compile(self, staff: StaffComposition, *, generation: int = 0, metadata: Mapping[str, Any] | None = None) -> CompositionSnapshot:
        visible = staff.visible_resource_ids()
        grants: list[CapabilityGrant] = []

        def provider_for(op: str, binding_metadata: Mapping[str, Any] | None = None) -> tuple[str | None, str | None]:
            """Resolve the provider module id/version for ``op``.

            Precedence: per-binding ``provider_module_id`` (Staff or SOP-level pin) > the registry's
            default provider for the operation. Falls back to ``(None, None)`` when no pin and no
            registry is reachable (unit tests), meaning ``for_operation`` decides at dispatch time.
            """

            pin = str((binding_metadata or {}).get("provider_module_id") or "").strip() or None
            if pin:
                # version is informational; it rides the grant so a mid-turn registry swap of the same
                # pin is detectable (the snapshot is the frozen contract).
                from staffdeck_harness.modules.registry import peek_registry

                reg = peek_registry()
                installed = reg.get(pin) if reg is not None else None
                if installed is not None:
                    return pin, installed.manifest.version
                return pin, None
            from staffdeck_harness.modules.registry import peek_registry

            reg = peek_registry()
            default = reg.for_operation(op) if reg is not None else None
            if default is not None:
                return default.manifest.module_id, default.manifest.version
            return None, None

        # L1 direct capabilities: available in ordinary conversation.
        for cap in staff.capabilities:
            op = {
                "knowledge_base": "knowledge.search/v1",
                "general_skill": "general_skill.consume/v1",
                "tool": "tool.invoke/v1",
                "mcp_server": "mcp.invoke/v1",
            }[cap.resource_type]
            if cap.capability_scope == "sop_specific":
                continue  # only reachable through a SOP slot
            mid, ver = provider_for(op, cap.metadata)
            grants.append(CapabilityGrant(operation=op, resource_type=cap.resource_type, resource_id=cap.resource_id, name=cap.name, binding_id=cap.binding_id, scope="general", provider_module_id=mid, provider_version=ver))

        # L2 SOPs: logical slots -> Staff bindings, plus sub-SOP graph validation.
        sop_edges: dict[str, list[str]] = {}
        plans: list[SopExecutionPlan] = []
        cap_names = {(c.resource_type, c.resource_id): c.name for c in staff.capabilities}
        cap_binding = {(c.resource_type, c.resource_id): c.binding_id for c in staff.capabilities}
        cap_metadata = {(c.resource_type, c.resource_id): c.metadata for c in staff.capabilities}
        for sop in staff.sops:
            _check_contracts(d.operation for d in sop.declared_slots)
            resolved = resolve_slots(
                sop.declared_slots,
                sop.slot_bindings,
                visible_resources={k: v for k, v in visible.items() if k in {"knowledge_base", "general_skill", "tool"}},
            )
            subs = _sub_sop_ids(sop.content)
            sop_edges[sop.skill_id] = list(subs)
            plans.append(SopExecutionPlan(skill_id=sop.skill_id, version=sop.version, name=sop.name, content=sop.content, resolved_slots=tuple(resolved), sub_sop_ids=subs))
            for slot in resolved:
                if slot.resource_type in {"handoff", "skill", "capability"}:
                    continue
                key = (slot.resource_type, slot.resource_id)
                mid, ver = provider_for(slot.declaration.operation, cap_metadata.get(key))
                grants.append(
                    CapabilityGrant(
                        operation=slot.declaration.operation,
                        resource_type=slot.resource_type,
                        resource_id=slot.resource_id,
                        name=cap_names.get(key, slot.resource_id),
                        binding_id=slot.binding_id or cap_binding.get(key),
                        scope="sop_specific",
                        sop_id=sop.skill_id,
                        node_id=slot.declaration.node_id,
                        slot_name=slot.declaration.name,
                        required=slot.declaration.required,
                        provider_module_id=mid,
                        provider_version=ver,
                    )
                )
        _detect_cycle(sop_edges, error=DependencyCycle, what="sub-SOP")

        hooks = compile_hooks(self.hooks)
        payload = {
            "tenant_id": staff.tenant_id,
            "staff_id": staff.staff_id,
            "persona": staff.persona,
            "model_route": dict(sorted(staff.model_route.items())),
            "session_policy": asdict(staff.session_policy),
            "grants": sorted((asdict(g) for g in grants), key=lambda g: (g["scope"], g["operation"], g["resource_id"], g["sop_id"] or "", g["node_id"] or "")),
            "sops": sorted(({"skill_id": p.skill_id, "version": p.version, "slots": sorted((s.declaration.name, s.resource_id) for s in p.resolved_slots)} for p in plans), key=lambda p: p["skill_id"]),
            "hooks": {k: [c.handler for c in v] for k, v in hooks.order.items()},
            "channels": sorted(c.binding_id for c in staff.channels),
            "team_id": staff.team.team_id if staff.team else None,
            "interactions": list(staff.interactions),
        }
        snapshot_id = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        return CompositionSnapshot(
            snapshot_id=snapshot_id,
            tenant_id=staff.tenant_id,
            staff_id=staff.staff_id,
            staff_ref_attributes=dict(staff.ref.attributes),
            persona=staff.persona,
            model_route=dict(staff.model_route),
            session_policy=asdict(staff.session_policy),
            grants=tuple(grants),
            sops=tuple(plans),
            hooks=hooks,
            channels=tuple(c.binding_id for c in staff.channels),
            team_id=staff.team.team_id if staff.team else None,
            interactions=tuple(staff.interactions),
            generation=generation,
            metadata=dict(metadata or {}),
        )
