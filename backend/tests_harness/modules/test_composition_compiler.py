"""composition.compiler — kernel module validating a StaffComposition and freezing it into a CompositionSnapshot."""

from __future__ import annotations

import re

import pytest

from app.db.models import AgentResourceBinding, KnowledgeBase, Skill
from staffdeck_harness.composition.compiler import DEFAULT_HOOKS, CompositionSnapshot
from staffdeck_harness.composition.slots import sop_slots
from staffdeck_harness.composition.staff import CapabilityBindingView, SessionPolicy, SopView, StaffComposition
from staffdeck_harness.contracts.errors import DependencyCycle, PermissionDenied, RequiredSlotMissing, SlotNotBound
from staffdeck_harness.contracts.manifest import HOOK_POINTS, ModuleKind, SlotName
from staffdeck_harness.contracts.security import ResourceRef
from staffdeck_harness.modules.kernel import CompositionCompilerModule
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "composition.compiler"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def _ref(t, i, **attrs):
    return ResourceRef(type=t, id=i, tenant_id="t1", attributes=attrs)


def _cap(t, i, scope="general"):
    return CapabilityBindingView(resource_type=t, resource_id=i, binding_id=f"b_{i}", ref=_ref(t, i), name=i, capability_scope=scope)


def _sop(skill_id, nodes, slot_bindings=None):
    content = {"nodes": nodes, "edges": [], "start_node_id": nodes[0]["node_id"] if nodes else None}
    return SopView(skill_id=skill_id, row_id=f"row_{skill_id}", version="1.0.0", name=skill_id, content=content, binding_id=f"sb_{skill_id}", slot_bindings=dict(slot_bindings or {}), declared_slots=tuple(sop_slots(content)), ref=_ref("sop", skill_id))


def _staff(caps=(), sops=(), persona="你是A"):
    return StaffComposition(tenant_id="t1", staff_id="a1", name="A", is_overall=False, status="active", persona=persona, model_route={"default": "m1"}, session_policy=SessionPolicy(), capabilities=tuple(caps), sops=tuple(sops), channels=(), team=None, interactions=("sop_adapter",), ref=_ref("agent", "a1", owner_user_id="u1"))


def test_manifest_composition_compiler(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.KERNEL
    assert item.slot is SlotName.STAFF_SOP
    assert m.attaches_to == (SlotName.STAFF_SOP,)
    assert isinstance(item.provider, CompositionCompilerModule)
    assert item.provider.module_id == MODULE_ID

    d = _described(registry)
    assert d["kind"] == "K"
    assert d["slot"] == "staff.sop"
    assert d["provides"] == []
    assert d["requires"] == []
    assert d["policy_actions"] == []
    assert d["hooks"] == []
    assert d["name"] == "配置校验与发布"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["enabled"] is True
    assert d["source"] == "builtin"
    assert d["guarded"] == (item.slot in registry._guarded_slots)
    # the requirement is satisfied by the interaction package that contributes hooks
    supplier = registry.for_operation("hook.contribute/v1")
    assert supplier is not None and supplier.enabled
    assert supplier.slot is SlotName.STAFF_INTERACTION


def test_manifest_composition_compiler_accepts_an_explicit_empty_hook_plan(settings):
    """PEP and ledger are host invariants; they do not require an optional interaction plugin."""

    reg = discover_and_install(ModuleRegistry(), settings)
    for slot in SlotName:
        reg.mark_guarded(slot)
    suppliers = [i.manifest.module_id for i in reg.providers(SlotName.STAFF_INTERACTION) if "hook.contribute/v1" in i.manifest.provides_operations]
    assert suppliers
    for mid in suppliers:
        reg.set_enabled(mid, False)
    reg.seal()
    assert reg.hooks() == ()


def test_placement_composition_compiler(registry):
    d = _described(registry)
    assert d["switchable"] is False
    nodes = tree(registry.describe())
    staff = next(b for b in nodes if b["id"] == "staff")
    sub = next(s for s in staff["subs"] if s["id"] == "staff.publish")
    placed = [m for m in sub["modules"] if m["module_id"] == MODULE_ID]
    assert len(placed) == 1
    assert placed[0]["placement"] == {"big_id": "staff", "sub_id": "staff.publish", "source": "taxonomy"}
    assert placed[0]["movable"] is False
    # staff.publish is a module-id-only sub (no slot); the compiler is its only member
    assert sub["slots"] == [] and sub["kind"] == "K"
    assert [m["module_id"] for m in sub["modules"]] == [MODULE_ID]


def test_disable_composition_compiler(registry, settings):
    d = _described(registry)
    assert d["kind"] == "K" and d["switchable"] is False
    assert d["slot"] not in {"runtime.engine", "security.pep"}
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is True  # kernel modules ignore disabled_modules


def test_provider_composition_compiler_snapshot_is_deterministic(module):
    provider = module(MODULE_ID).provider
    node = {"node_id": "n1", "metadata": {"slots": [{"name": "docs", "operation": "knowledge.search/v1", "required": True}]}}
    staff = _staff(caps=[_cap("knowledge_base", "kb1"), _cap("tool", "tool1"), _cap("tool", "tool_sop", scope="sop_specific")], sops=[_sop("sop1", [node], {"docs": "kb1"})])

    snap = provider.compile(staff, generation=3, metadata={"note": "x"})
    assert isinstance(snap, CompositionSnapshot)
    assert SHA256.match(snap.snapshot_id)
    assert snap.tenant_id == "t1" and snap.staff_id == "a1" and snap.persona == "你是A"
    assert dict(snap.model_route) == {"default": "m1"}
    assert snap.generation == 3 and dict(snap.metadata) == {"note": "x"}
    assert snap.staff_ref_attributes == {"owner_user_id": "u1"}
    assert snap.interactions == ("sop_adapter",) and snap.channels == () and snap.team_id is None

    general = {(g.operation, g.resource_id) for g in snap.grants if g.scope == "general"}
    assert general == {("knowledge.search/v1", "kb1"), ("tool.invoke/v1", "tool1")}  # sop_specific capability not granted generally
    sop_grants = [g for g in snap.grants if g.scope == "sop_specific"]
    assert [(g.operation, g.resource_id, g.sop_id, g.node_id, g.slot_name, g.required) for g in sop_grants] == [("knowledge.search/v1", "kb1", "sop1", "n1", "docs", True)]
    assert snap.allowed_resource_ids() == {"knowledge_base": {"kb1"}, "tool": {"tool1"}}
    assert snap.allowed_resource_ids(sop_id="sop1", node_id="n1")["knowledge_base"] == {"kb1"}
    assert snap.sop("sop1") is not None and snap.sop("sop1").resolved_slots[0].resource_id == "kb1"
    assert snap.sop("nope") is None

    # hook plan covers every hook point, in the shipped default order (registry hooks == DEFAULT_HOOKS)
    assert set(snap.hooks.order) == set(HOOK_POINTS)
    assert [c.handler for c in snap.hooks.order["pre_tool"]] == ["activation.allowlist", "capability.pep"]
    assert {c.handler for pt in snap.hooks.order.values() for c in pt} == {h.handler for h in DEFAULT_HOOKS}

    # same composition -> same id; any binding change -> new id; generation/metadata do not affect identity
    assert provider.compile(staff, generation=9).snapshot_id == snap.snapshot_id
    changed = _staff(caps=[_cap("knowledge_base", "kb1")], sops=[_sop("sop1", [node], {"docs": "kb1"})])
    assert provider.compile(changed).snapshot_id != snap.snapshot_id


def test_provider_composition_compiler_publish_validation(module):
    provider = module(MODULE_ID).provider
    node = {"node_id": "n1", "metadata": {"slots": [{"name": "docs", "operation": "knowledge.search/v1", "required": True}]}}
    with pytest.raises(RequiredSlotMissing):
        provider.compile(_staff(caps=[_cap("knowledge_base", "kb1")], sops=[_sop("sop1", [node])]))
    with pytest.raises(SlotNotBound):
        provider.compile(_staff(caps=[_cap("knowledge_base", "kb1")], sops=[_sop("sop1", [node], {"docs": "kb_hidden"})]))
    a = _sop("a", [{"node_id": "n1", "sub_sop_id": "b"}])
    b = _sop("b", [{"node_id": "n1", "sub_sop_id": "a"}])
    with pytest.raises(DependencyCycle):
        provider.compile(_staff(sops=[a, b]))
    # a plain staff with no bindings compiles to an empty-but-valid snapshot
    empty = provider.compile(_staff())
    assert empty.grants == () and empty.sops == () and SHA256.match(empty.snapshot_id)


def test_provider_composition_compiler_from_projection(registry, db):
    """End-to-end within the kernel: composition.projection -> composition.compiler on real rows."""

    project = registry.get("composition.projection").provider.project
    compile_ = registry.get(MODULE_ID).provider
    db.add(KnowledgeBase(id="kb1", tenant_id="t1", name="KB"))
    db.add(AgentResourceBinding(id="b_kb1", tenant_id="t1", agent_id="a1", resource_type="knowledge_base", resource_id="kb1", metadata_json={"scope": "agent_private"}))
    content = {"nodes": [{"node_id": "n1", "metadata": {"slots": [{"name": "docs", "operation": "knowledge.search/v1", "required": True}]}}], "edges": [], "start_node_id": "n1"}
    db.add(Skill(id="sop1", tenant_id="t1", skill_id="sop1", name="SOP1", content_json=content, status="published"))
    db.add(AgentResourceBinding(id="b_sop1", tenant_id="t1", agent_id="a1", resource_type="skill", resource_id="sop1", metadata_json={"scope": "agent_private", "slot_bindings": {"docs": "kb1"}}))
    db.commit()

    snap = compile_.compile(project(db, "t1", "a1"), generation=registry.generation)
    assert SHA256.match(snap.snapshot_id) and snap.generation == registry.generation
    assert snap.persona and "Agent One" in snap.persona
    assert dict(snap.model_route) == {"default": "m1"}
    assert {(g.scope, g.operation, g.resource_id, g.binding_id) for g in snap.grants} == {("general", "knowledge.search/v1", "kb1", "b_kb1"), ("sop_specific", "knowledge.search/v1", "kb1", "b_kb1")}
    assert [p.skill_id for p in snap.sops] == ["sop1"]
    assert snap.sops[0].resolved_slots[0].resource_id == "kb1"


def test_events_or_pep_composition_compiler_grants_are_pep_checked_downstream(module, guard, security_ctx):
    """The compiler is unguarded (no policy actions): grants only *narrow* what the guarded capability host may allow."""

    provider = module(MODULE_ID).provider
    snap = provider.compile(_staff(caps=[_cap("knowledge_base", "kb1")]))
    grant = snap.grants[0]
    g = guard(MODULE_ID)
    ok = ResourceRef(type=grant.resource_type, id=grant.resource_id, tenant_id="t1", attributes={"binding_status": "active", "private_to_agent": True})
    assert g.require(security_ctx(), grant.operation, ok).allowed
    foreign = ResourceRef(type=grant.resource_type, id=grant.resource_id, tenant_id="t2", attributes={"binding_status": "active", "private_to_agent": True})
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), grant.operation, foreign)
