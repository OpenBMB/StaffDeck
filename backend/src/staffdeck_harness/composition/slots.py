"""Logical SOP capability slots and Staff slot bindings.

The architecture forbids a SOP from naming concrete knowledge bases, tools or
skills; it declares *logical slots* (``policy_docs -> knowledge.search/v1``)
and the Staff that installs the SOP binds each slot to a concrete resource.

Legacy SOP nodes carry concrete ids in ``capability_refs``. This module supports
both, without changing stored SOPs:

- **Declared slots** live in ``node.metadata.slots``::

      {"slots": [{"name": "policy_docs", "operation": "knowledge.search/v1", "required": true}]}

- **Implicit slots** are synthesized from ``capability_refs`` (one slot per
  concrete id, named ``kb:<id>`` etc.) and bound to that same id, so existing
  SOPs compile unchanged.

- **Staff slot bindings** live in ``AgentResourceBinding.metadata_json.slot_bindings``
  on the SOP's own binding row (resource_type ``skill``)::

      {"slot_bindings": {"policy_docs": "kb_123", "order_query": "tool_456"}}

The same SOP therefore attaches to two Staff with different bindings, which is
one of the acceptance criteria.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from staffdeck_harness.contracts.errors import RequiredSlotMissing, SlotKindMismatch, SlotNotBound
from staffdeck_harness.contracts.manifest import SLOT_FOR_OPERATION, SlotName

# capability_refs field -> operation
_IMPLICIT_OPERATION = {
    "knowledge_base_ids": "knowledge.search/v1",
    "general_skill_ids": "general_skill.consume/v1",
    "tool_ids": "tool.invoke/v1",
}
_IMPLICIT_PREFIX = {
    "knowledge_base_ids": "kb",
    "general_skill_ids": "skill",
    "tool_ids": "tool",
}
_REQUIRED_FIELD = {
    "knowledge_base_ids": "required_knowledge_base_ids",
    "general_skill_ids": "required_general_skill_ids",
    "tool_ids": "required_tool_ids",
}

# operation -> resource type the bound resource must have
RESOURCE_TYPE_FOR_OPERATION: dict[str, str] = {
    "knowledge.search/v1": "knowledge_base",
    "general_skill.consume/v1": "general_skill",
    "tool.invoke/v1": "tool",
    "mcp.invoke/v1": "tool",
    "a2a.invoke/v1": "tool",
    "sandbox.execute/v1": "capability",
    "artifact.publish/v1": "capability",
    "handoff.request/v1": "handoff",
    "sop.execute/v1": "skill",
}


@dataclass(frozen=True)
class SlotDeclaration:
    name: str
    operation: str
    required: bool = False
    node_id: str | None = None
    implicit: bool = False
    hint: str | None = None

    @property
    def slot(self) -> SlotName:
        from staffdeck_harness.modules.registry import peek_registry

        reg = peek_registry()
        if reg and self.operation in reg.operations:
            return reg.operations[self.operation].slot
        return SLOT_FOR_OPERATION[self.operation]

    @property
    def resource_type(self) -> str:
        from staffdeck_harness.modules.registry import peek_registry

        reg = peek_registry()
        if reg and self.operation in reg.operations:
            return reg.operations[self.operation].resource_type
        return RESOURCE_TYPE_FOR_OPERATION[self.operation]


@dataclass(frozen=True)
class ResolvedSlot:
    declaration: SlotDeclaration
    resource_id: str
    resource_type: str
    binding_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def declared_slots(node: Mapping[str, Any]) -> list[SlotDeclaration]:
    """Slots declared on one SOP node, both explicit (metadata.slots) and implicit (capability_refs)."""

    node_id = str(node.get("node_id") or node.get("step_id") or "") or None
    out: list[SlotDeclaration] = []
    meta = node.get("metadata") or {}
    for raw in (meta.get("slots") or []):
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or "").strip()
        operation = str(raw.get("operation") or "").strip()
        from staffdeck_harness.modules.registry import peek_registry

        reg = peek_registry()
        if not name or (operation not in SLOT_FOR_OPERATION and not (reg and operation in reg.operations)):
            raise SlotKindMismatch(f"node {node_id}: slot {name!r} has unknown operation {operation!r}")
        out.append(SlotDeclaration(name=name, operation=operation, required=bool(raw.get("required", False)), node_id=node_id, hint=raw.get("hint")))
    refs = node.get("capability_refs") or {}
    for field_name, operation in _IMPLICIT_OPERATION.items():
        required_ids = set(refs.get(_REQUIRED_FIELD[field_name]) or [])
        for rid in refs.get(field_name) or []:
            rid = str(rid)
            out.append(
                SlotDeclaration(
                    name=f"{_IMPLICIT_PREFIX[field_name]}:{rid}",
                    operation=operation,
                    required=rid in required_ids,
                    node_id=node_id,
                    implicit=True,
                )
            )
    if node.get("type") in {"human_handoff", "handoff"} or node.get("assignee_user_id"):
        out.append(SlotDeclaration(name=f"handoff:{node_id}", operation="handoff.request/v1", node_id=node_id, implicit=True))
    if node.get("sub_sop_id"):
        out.append(SlotDeclaration(name=f"subsop:{node['sub_sop_id']}", operation="sop.execute/v1", required=True, node_id=node_id, implicit=True))
    return out


def sop_slots(content: Mapping[str, Any]) -> list[SlotDeclaration]:
    nodes = content.get("nodes") or content.get("steps") or []
    out: list[SlotDeclaration] = []
    for node in nodes:
        if isinstance(node, Mapping):
            out.extend(declared_slots(node))
    return out


def resolve_slots(
    declarations: Iterable[SlotDeclaration],
    staff_bindings: Mapping[str, str | Mapping[str, Any]],
    *,
    visible_resources: Mapping[str, set[str]] | None = None,
    binding_ids: Mapping[str, str] | None = None,
) -> list[ResolvedSlot]:
    """Bind declared slots to concrete resources for one Staff.

    ``staff_bindings``  slot name -> resource id (from the Staff's SOP binding metadata).
    ``visible_resources`` resource type -> ids the Staff may use; when given, a
    binding to an invisible resource is rejected here so the PEP never sees it.
    """

    resolved: list[ResolvedSlot] = []
    for decl in declarations:
        raw = staff_bindings.get(f"{decl.node_id}/{decl.name}", staff_bindings.get(decl.name))
        binding = dict(raw) if isinstance(raw, Mapping) else {}
        if decl.implicit:
            resource_id = str(binding.get("resource_id") or raw or decl.name.split(":", 1)[-1])
        else:
            resource_id = str(binding.get("resource_id") or (raw if isinstance(raw, str) else "") or "").strip()
        if not resource_id:
            if decl.required:
                raise RequiredSlotMissing(f"required slot {decl.name!r} ({decl.operation}) is not bound", details={"slot": decl.name, "node_id": decl.node_id})
            continue
        rtype = decl.resource_type
        if visible_resources is not None and rtype in visible_resources and resource_id not in visible_resources[rtype]:
            if decl.required:
                raise SlotNotBound(f"slot {decl.name!r} is bound to {rtype} {resource_id!r} which this staff cannot use", details={"slot": decl.name, "resource": resource_id})
            continue
        resolved.append(
            ResolvedSlot(
                declaration=decl,
                resource_id=resource_id,
                resource_type=rtype,
                binding_id=(binding_ids or {}).get(resource_id),
                metadata=binding,
            )
        )
    return resolved
