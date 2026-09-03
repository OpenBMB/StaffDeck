"""Module tests for ``sop.definition`` (流程定义, kind C, slot sop.slot.control).

Provider: ``staffdeck_harness.modules.kernel.SopDefinitionModule`` — parses the
logical capability slots declared by a SOP content document.
"""

from __future__ import annotations

import re

import pytest

from staffdeck_harness.composition.slots import SlotDeclaration
from staffdeck_harness.contracts.errors import PermissionDenied, SlotKindMismatch
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.modules.kernel import SopDefinitionModule
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "sop.definition"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def _placed(registry, module_id: str = MODULE_ID) -> dict:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return m
    raise AssertionError(f"{module_id} not found in taxonomy tree")


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_sop_definition(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind.value == "C"
    assert item.slot is SlotName.SOP_SLOT_CONTROL
    assert m.attaches_to == (SlotName.SOP_SLOT_CONTROL,)
    assert m.provides_operations == ("sop.execute/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ("sop.execute/v1",)
    assert m.hooks == ()
    assert isinstance(item.provider, SopDefinitionModule)
    assert item.provider.module_id == MODULE_ID

    d = _described(registry)
    assert d["name"] == "流程定义"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["enabled"] is True
    assert d["source"] == "builtin"
    # declares policy actions -> must sit on a PEP-bound slot
    assert d["policy_actions"] == ["sop.execute/v1"]
    assert d["guarded"] is True


def test_manifest_guarded_by_builtin_registration(settings):
    """Even without the fixture marking every slot, builtin registration guards sop.slot.control."""

    reg = discover_and_install(ModuleRegistry(), settings)
    d = _described(reg)
    assert d["policy_actions"] and d["guarded"] is True


# --------------------------------------------------------------------------- 2. placement

def test_placement_sop_definition(registry):
    d = _described(registry)
    assert d["switchable"] is False  # content package, not a code plugin
    placed = _placed(registry)
    assert placed["placement"] == {"big_id": "sop", "sub_id": "sop.definition", "source": "taxonomy"}
    assert placed["movable"] is True  # C modules may be re-parented for display


# --------------------------------------------------------------------------- 3. disable

def test_disable_sop_definition_not_switchable(registry):
    """Admin API refuses to disable non-switchable modules (``switchable`` False in describe())."""

    d = _described(registry)
    assert d["kind"] == "C"
    assert d["switchable"] is False
    assert d["slot"] not in {"runtime.engine", "security.pep"}


# --------------------------------------------------------------------------- 4. provider

def test_provider_sop_definition_slots_explicit_and_implicit(module):
    provider = module(MODULE_ID).provider
    content = {
        "start_node_id": "n1",
        "nodes": [
            {
                "node_id": "n1",
                "name": "查政策",
                "metadata": {"slots": [{"name": "policy_docs", "operation": "knowledge.search/v1", "required": True, "hint": "政策库"}]},
                "capability_refs": {"tool_ids": ["tool_1"], "required_tool_ids": ["tool_1"], "knowledge_base_ids": ["kb_9"]},
            },
            {"node_id": "n2", "type": "human_handoff"},
            {"node_id": "n3", "sub_sop_id": "sop_child"},
        ],
        "edges": [],
    }
    slots = provider.slots(content)
    assert all(isinstance(s, SlotDeclaration) for s in slots)
    by_name = {s.name: s for s in slots}
    assert set(by_name) == {"policy_docs", "tool:tool_1", "kb:kb_9", "handoff:n2", "subsop:sop_child"}

    explicit = by_name["policy_docs"]
    assert explicit.operation == "knowledge.search/v1" and explicit.required is True and explicit.implicit is False
    assert explicit.node_id == "n1" and explicit.hint == "政策库"
    assert explicit.slot is SlotName.SOP_SLOT_KNOWLEDGE and explicit.resource_type == "knowledge_base"

    assert by_name["tool:tool_1"].required is True and by_name["tool:tool_1"].implicit is True
    assert by_name["kb:kb_9"].required is False and by_name["kb:kb_9"].implicit is True
    assert by_name["handoff:n2"].operation == "handoff.request/v1" and by_name["handoff:n2"].resource_type == "handoff"
    sub = by_name["subsop:sop_child"]
    assert sub.operation == "sop.execute/v1" and sub.required is True and sub.slot is SlotName.SOP_SLOT_CONTROL


def test_provider_sop_definition_accepts_steps_alias_and_empty(module):
    provider = module(MODULE_ID).provider
    assert provider.slots({}) == []
    assert provider.slots({"nodes": ["not-a-node", 3]}) == []
    slots = provider.slots({"steps": [{"step_id": "s1", "capability_refs": {"general_skill_ids": ["gs1"]}}]})
    assert [(s.name, s.operation, s.node_id) for s in slots] == [("skill:gs1", "general_skill.consume/v1", "s1")]


def test_provider_sop_definition_rejects_unknown_operation(module):
    provider = module(MODULE_ID).provider
    with pytest.raises(SlotKindMismatch):
        provider.slots({"nodes": [{"node_id": "n1", "metadata": {"slots": [{"name": "x", "operation": "nope/v9"}]}}]})


# --------------------------------------------------------------------------- 5. PEP

def test_pep_sop_definition_denies_cross_tenant(guard, security_ctx, module):
    m = module(MODULE_ID).manifest
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    for op in m.policy_actions:
        assert mapper.map(op) == ("execute", "sop")
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="sop", id="sop_x", tenant_id="t2", attributes={"owner_user_id": "u1"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), "sop.execute/v1", foreign)
    assert exc.value.details["operation"] == "sop.execute/v1"
    assert exc.value.details["profile"] == "OSS_LOCAL"
    # same-tenant owner is allowed through the same action mapping
    own = ResourceRef(type="sop", id="sop_x", tenant_id="t1", attributes={"owner_user_id": "u1"})
    assert g.require(security_ctx(), "sop.execute/v1", own).allowed is True
