from __future__ import annotations

import pytest

from staffdeck_harness.composition import (
    CompositionCompiler,
    SessionPolicy,
    SopView,
    StaffComposition,
    CapabilityBindingView,
    compile_hooks,
    declared_slots,
    resolve_slots,
)
from staffdeck_harness.contracts import (
    DependencyCycle,
    HookContribution,
    HookCycle,
    RequiredSlotMissing,
    ResourceRef,
    SlotNotBound,
)


def _ref(t, i, **attrs):
    return ResourceRef(type=t, id=i, tenant_id="t1", attributes=attrs)


def _cap(t, i, name=None, scope="general"):
    return CapabilityBindingView(resource_type=t, resource_id=i, binding_id=f"b_{i}", ref=_ref(t, i), name=name or i, capability_scope=scope)


def _sop(skill_id, nodes, slot_bindings=None, version="1.0.0"):
    content = {"nodes": nodes, "edges": [], "start_node_id": nodes[0]["node_id"] if nodes else None}
    from staffdeck_harness.composition.slots import sop_slots

    return SopView(
        skill_id=skill_id, row_id=f"row_{skill_id}", version=version, name=skill_id, content=content,
        binding_id=f"sb_{skill_id}", slot_bindings=dict(slot_bindings or {}), declared_slots=tuple(sop_slots(content)),
        ref=_ref("sop", skill_id),
    )


def _staff(caps=(), sops=()):
    return StaffComposition(
        tenant_id="t1", staff_id="a1", name="A", is_overall=False, status="active", persona="你是A",
        model_route={"default": "m1"}, session_policy=SessionPolicy(), capabilities=tuple(caps), sops=tuple(sops),
        channels=(), team=None, interactions=("sop_adapter",), ref=_ref("agent", "a1", owner_user_id="u1"),
    )


def test_declared_slots_explicit_and_implicit() -> None:
    node = {
        "node_id": "n1",
        "metadata": {"slots": [{"name": "policy_docs", "operation": "knowledge.search/v1", "required": True}]},
        "capability_refs": {"tool_ids": ["tool_a"], "required_tool_ids": ["tool_a"], "knowledge_base_ids": ["kb_z"]},
        "sub_sop_id": "child",
    }
    slots = declared_slots(node)
    names = {s.name: s for s in slots}
    assert names["policy_docs"].required and not names["policy_docs"].implicit
    assert names["tool:tool_a"].implicit and names["tool:tool_a"].required
    assert names["kb:kb_z"].implicit and not names["kb:kb_z"].required
    assert names["subsop:child"].operation == "sop.execute/v1"


def test_resolve_slots_requires_bindings_and_visibility() -> None:
    node = {"node_id": "n1", "metadata": {"slots": [{"name": "policy_docs", "operation": "knowledge.search/v1", "required": True}]}}
    decl = declared_slots(node)
    with pytest.raises(RequiredSlotMissing):
        resolve_slots(decl, {})
    with pytest.raises(SlotNotBound):
        resolve_slots(decl, {"policy_docs": "kb_hidden"}, visible_resources={"knowledge_base": {"kb_ok"}})
    resolved = resolve_slots(decl, {"policy_docs": "kb_ok"}, visible_resources={"knowledge_base": {"kb_ok"}})
    assert resolved[0].resource_id == "kb_ok"


def test_same_sop_two_staffs_different_bindings() -> None:
    node = {"node_id": "n1", "metadata": {"slots": [{"name": "policy_docs", "operation": "knowledge.search/v1", "required": True}]}}
    sop_a = _sop("sop1", [node], {"policy_docs": "kb_a"})
    sop_b = _sop("sop1", [node], {"policy_docs": "kb_b"})
    snap_a = CompositionCompiler().compile(_staff([_cap("knowledge_base", "kb_a")], [sop_a]))
    snap_b = CompositionCompiler().compile(_staff([_cap("knowledge_base", "kb_b")], [sop_b]))
    assert snap_a.snapshot_id != snap_b.snapshot_id
    assert snap_a.allowed_resource_ids(sop_id="sop1")["knowledge_base"] == {"kb_a"}
    assert snap_b.allowed_resource_ids(sop_id="sop1")["knowledge_base"] == {"kb_b"}


def test_snapshot_is_deterministic_and_changes_on_binding_change() -> None:
    staff = _staff([_cap("tool", "t1")])
    a = CompositionCompiler().compile(staff)
    b = CompositionCompiler().compile(staff)
    assert a.snapshot_id == b.snapshot_id
    c = CompositionCompiler().compile(_staff([_cap("tool", "t1"), _cap("tool", "t2")]))
    assert c.snapshot_id != a.snapshot_id


def test_sop_specific_capability_only_reachable_through_sop() -> None:
    node = {"node_id": "n1", "capability_refs": {"tool_ids": ["t_sop"]}}
    staff = _staff([_cap("tool", "t_gen"), _cap("tool", "t_sop", scope="sop_specific")], [_sop("sop1", [node])])
    snap = CompositionCompiler().compile(staff)
    assert snap.allowed_resource_ids()["tool"] == {"t_gen"}
    assert snap.allowed_resource_ids(sop_id="sop1", node_id="n1")["tool"] == {"t_gen", "t_sop"}


def test_sub_sop_cycle_is_rejected() -> None:
    a = _sop("A", [{"node_id": "n1", "sub_sop_id": "B"}])
    b = _sop("B", [{"node_id": "n1", "sub_sop_id": "A"}])
    with pytest.raises(DependencyCycle):
        CompositionCompiler().compile(_staff([], [a, b]))


def test_contract_incompatible_is_rejected() -> None:
    node = {"node_id": "n1", "metadata": {"slots": [{"name": "x", "operation": "knowledge.search/v9"}]}}
    from staffdeck_harness.contracts import SlotKindMismatch

    with pytest.raises(SlotKindMismatch):
        _sop("S", [node])


def test_hook_cycle_is_rejected_and_order_respects_dependencies() -> None:
    hooks = (
        HookContribution(point="pre_step", handler="a", order=50, depends_on=("b",)),
        HookContribution(point="pre_step", handler="b", order=10),
    )
    plan = compile_hooks(hooks)
    assert [c.handler for c in plan.order["pre_step"]] == ["b", "a"]
    cyc = (
        HookContribution(point="pre_step", handler="a", depends_on=("b",)),
        HookContribution(point="pre_step", handler="b", depends_on=("a",)),
    )
    with pytest.raises(HookCycle):
        compile_hooks(cyc)
