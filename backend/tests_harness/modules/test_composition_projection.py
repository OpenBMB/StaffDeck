"""composition.projection — kernel module projecting one employee (persona, models, bindings, SOPs, channels, team)."""

from __future__ import annotations

import re

import pytest

from app.db.models import AgentResourceBinding, ChannelBinding, KnowledgeBase, Skill, Team, TeamMember, Tool
from staffdeck_harness.composition.staff import DEFAULT_INTERACTIONS, SessionPolicy, StaffComposition
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import ResourceRef
from staffdeck_harness.modules.kernel import CompositionProjectionModule
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "composition.projection"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def _bind_private_kb(db, kb_id: str = "kb1", binding_id: str = "b_kb1") -> None:
    db.add(KnowledgeBase(id=kb_id, tenant_id="t1", name=f"KB {kb_id}"))
    db.add(AgentResourceBinding(id=binding_id, tenant_id="t1", agent_id="a1", resource_type="knowledge_base", resource_id=kb_id, metadata_json={"scope": "agent_private"}))


def test_manifest_composition_projection(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.KERNEL
    assert item.slot is SlotName.STAFF_SOP
    assert m.attaches_to == (SlotName.STAFF_SOP,)
    assert isinstance(item.provider, CompositionProjectionModule)
    assert item.provider.module_id == MODULE_ID

    d = _described(registry)
    assert d["kind"] == "K"
    assert d["slot"] == "staff.sop"
    assert d["provides"] == []
    assert d["requires"] == []
    assert d["policy_actions"] == []
    assert d["hooks"] == []
    assert d["name"] == "员工配置汇总"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["enabled"] is True
    assert d["source"] == "builtin"
    assert d["guarded"] == (item.slot in registry._guarded_slots)


def test_placement_composition_projection(registry):
    d = _described(registry)
    assert d["switchable"] is False
    nodes = tree(registry.describe())
    staff = next(b for b in nodes if b["id"] == "staff")
    sub = next(s for s in staff["subs"] if s["id"] == "staff.binding")
    placed = [m for m in sub["modules"] if m["module_id"] == MODULE_ID]
    assert len(placed) == 1
    assert placed[0]["placement"] == {"big_id": "staff", "sub_id": "staff.binding", "source": "taxonomy"}
    assert placed[0]["movable"] is False
    assert sub["slots"] == ["staff.sop"]
    # an operator placement override never moves a kernel module
    overridden = tree(registry.describe(), {MODULE_ID: "staff.publish"})
    staff2 = next(b for b in overridden if b["id"] == "staff")
    binding_sub = next(s for s in staff2["subs"] if s["id"] == "staff.binding")
    assert any(m["module_id"] == MODULE_ID for m in binding_sub["modules"])


def test_disable_composition_projection(registry, settings):
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


def test_provider_composition_projection_projects_agent(module, db):
    provider = module(MODULE_ID).provider
    _bind_private_kb(db)
    # a tool bound to another agent must not leak into a1's composition
    db.add(Tool(id="tool9", tenant_id="t1", name="other-tool", method="GET", url="http://x"))
    db.add(AgentResourceBinding(id="b_t9", tenant_id="t1", agent_id="a-other", resource_type="tool", resource_id="tool9", metadata_json={"scope": "agent_private"}))
    # an inactive binding is excluded too
    db.add(KnowledgeBase(id="kb2", tenant_id="t1", name="KB kb2"))
    db.add(AgentResourceBinding(id="b_kb2", tenant_id="t1", agent_id="a1", resource_type="knowledge_base", resource_id="kb2", status="inactive", metadata_json={"scope": "agent_private"}))
    db.add(ChannelBinding(id="ch1", tenant_id="t1", agent_id="a1", channel="feishu", status="active", config_json={}))
    db.add(Team(id="team1", tenant_id="t1", name="Team", owner_user_id="u1", status="active"))
    db.add(TeamMember(team_id="team1", agent_id="a1", role="member"))
    db.commit()

    staff = provider.project(db, "t1", "a1")
    assert isinstance(staff, StaffComposition)
    assert staff.tenant_id == "t1" and staff.staff_id == "a1" and staff.name == "Agent One"
    assert staff.is_overall is False and staff.status == "active"
    assert isinstance(staff.persona, str) and "Agent One" in staff.persona
    assert dict(staff.model_route) == {"default": "m1"}
    assert isinstance(staff.session_policy, SessionPolicy) and staff.session_policy.max_actions == 32
    assert [(c.resource_type, c.resource_id, c.binding_id) for c in staff.capabilities] == [("knowledge_base", "kb1", "b_kb1")]
    cap = staff.capabilities[0]
    assert cap.ref.tenant_id == "t1" and cap.ref.attributes["binding_status"] == "active" and cap.ref.attributes["private_to_agent"] is True
    assert staff.capability_ids("knowledge_base") == {"kb1"}
    assert staff.visible_resource_ids() == {"knowledge_base": {"kb1"}, "skill": set()}
    assert staff.sops == ()
    assert [(c.binding_id, c.channel, c.status) for c in staff.channels] == [("ch1", "feishu", "active")]
    assert staff.team is not None and staff.team.team_id == "team1" and staff.team.role == "member"
    assert staff.interactions == DEFAULT_INTERACTIONS
    assert staff.ref == ResourceRef(type="agent", id="a1", tenant_id="t1", attributes={"owner_user_id": "u1", "is_overall": False, "published_to_gallery": False, "shared_user_ids": (), "status": "active"})
    assert dict(staff.metadata) == {"owner_user_id": "u1"}

    # projection is read-only and deterministic
    again = provider.project(db, "t1", "a1")
    assert again.capabilities == staff.capabilities and again.model_route == staff.model_route


def test_provider_composition_projection_overall_and_missing_agent(module, db):
    provider = module(MODULE_ID).provider
    with pytest.raises(LookupError):
        provider.project(db, "t1", "nope")
    # tenant without an overall agent: synthetic overall composition, nothing bound
    staff = provider.project(db, "t1", None)
    assert staff.is_overall is True and staff.staff_id == "t1:overall" and staff.name == "overall"
    assert staff.capabilities == () and staff.sops == () and staff.channels == () and staff.team is None
    assert staff.ref.attributes == {"is_overall": True}
    assert dict(staff.model_route) == {"default": "m1"}


def test_provider_composition_projection_sop_with_matching_row_id(module, db):
    """A published SOP bound to the agent shows up with its declared slots and per-staff slot bindings."""

    provider = module(MODULE_ID).provider
    _bind_private_kb(db)
    content = {"nodes": [{"node_id": "n1", "metadata": {"slots": [{"name": "docs", "operation": "knowledge.search/v1", "required": True}]}}], "edges": [], "start_node_id": "n1"}
    # legacy bindings key skills by the Skill *row* id; keep row id == skill_id so both lookups agree
    db.add(Skill(id="sop1", tenant_id="t1", skill_id="sop1", name="SOP1", content_json=content, status="published"))
    db.add(AgentResourceBinding(id="b_sop1", tenant_id="t1", agent_id="a1", resource_type="skill", resource_id="sop1", metadata_json={"scope": "agent_private", "slot_bindings": {"docs": "kb1"}}))
    db.commit()

    staff = provider.project(db, "t1", "a1")
    assert len(staff.sops) == 1
    sop = staff.sops[0]
    assert sop.skill_id == "sop1" and sop.row_id == "sop1" and sop.version == "1.0.0" and sop.name == "SOP1"
    assert sop.binding_id == "b_sop1"
    assert dict(sop.slot_bindings) == {"docs": "kb1"}
    assert [(d.name, d.operation, d.required, d.node_id) for d in sop.declared_slots] == [("docs", "knowledge.search/v1", True, "n1")]
    assert sop.ref.type == "sop" and sop.ref.attributes["binding_status"] == "active" and sop.ref.attributes["private_to_agent"] is True
    assert staff.visible_resource_ids()["skill"] == {"sop1"}


def test_provider_composition_projection_sop_binding_keyed_by_row_id(module, db):
    provider = module(MODULE_ID).provider
    _bind_private_kb(db)
    content = {"nodes": [{"node_id": "n1", "metadata": {"slots": [{"name": "docs", "operation": "knowledge.search/v1", "required": True}]}}], "edges": [], "start_node_id": "n1"}
    db.add(Skill(id="skill_row_1", tenant_id="t1", skill_id="sop1", name="SOP1", content_json=content, status="published"))
    db.add(AgentResourceBinding(id="b_sop1", tenant_id="t1", agent_id="a1", resource_type="skill", resource_id="skill_row_1", metadata_json={"scope": "agent_private", "slot_bindings": {"docs": "kb1"}}))
    db.commit()

    staff = provider.project(db, "t1", "a1")
    assert len(staff.sops) == 1
    sop = staff.sops[0]
    assert sop.row_id == "skill_row_1"
    assert sop.binding_id == "b_sop1"
    assert dict(sop.slot_bindings) == {"docs": "kb1"}


def test_events_or_pep_composition_projection_refs_feed_the_pep(module, db, guard, security_ctx):
    """The projection declares no actions itself; the ResourceRefs it emits are what the guarded hosts check."""

    provider = module(MODULE_ID).provider
    _bind_private_kb(db)
    db.commit()
    staff = provider.project(db, "t1", "a1")
    ref = staff.capabilities[0].ref
    g = guard(MODULE_ID)
    assert g.require(security_ctx(), "knowledge.search/v1", ref).allowed
    foreign = ResourceRef(type=ref.type, id=ref.id, tenant_id="t2", attributes=dict(ref.attributes))
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), "knowledge.search/v1", foreign)
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(tenant_id="t2"), "staff.use/v1", staff.ref)
