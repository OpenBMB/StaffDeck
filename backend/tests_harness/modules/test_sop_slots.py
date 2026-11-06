"""Module tests for ``sop.slots`` (流程能力关联, kind K, slot sop.slot.control).

Provider: ``staffdeck_harness.modules.kernel.SopSlotResolverModule`` — binds the
logical slots a SOP declares to the concrete resources one Staff has bound.
"""

from __future__ import annotations

import re

import pytest

from staffdeck_harness.composition.slots import ResolvedSlot, SlotDeclaration, sop_slots
from staffdeck_harness.contracts.errors import PermissionDenied, RequiredSlotMissing, SlotNotBound
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.modules.kernel import SopSlotResolverModule
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "sop.slots"
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


def _decls() -> list[SlotDeclaration]:
    content = {
        "nodes": [
            {
                "node_id": "n1",
                "metadata": {"slots": [
                    {"name": "policy_docs", "operation": "knowledge.search/v1", "required": True},
                    {"name": "order_query", "operation": "tool.invoke/v1"},
                ]},
                "capability_refs": {"tool_ids": ["tool_fixed"]},
            },
        ],
    }
    return sop_slots(content)


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_sop_slots(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind.value == "K"
    assert item.slot is SlotName.SOP_SLOT_CONTROL
    assert m.attaches_to == (SlotName.SOP_SLOT_CONTROL,)
    assert m.provides_operations == ()
    assert m.requires_operations == ()
    assert m.policy_actions == ()
    assert m.hooks == ()
    assert isinstance(item.provider, SopSlotResolverModule)
    assert item.provider.module_id == MODULE_ID

    d = _described(registry)
    assert d["name"] == "流程能力关联"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["enabled"] is True
    assert d["policy_actions"] == []
    # no own policy actions; ``guarded`` reflects the shared, PEP-bound sop.slot.control host
    assert d["guarded"] is True


def test_manifest_guarded_by_builtin_registration(settings):
    reg = discover_and_install(ModuleRegistry(), settings)
    d = _described(reg)
    assert d["guarded"] is True and d["policy_actions"] == []


# --------------------------------------------------------------------------- 2. placement

def test_placement_sop_slots(registry):
    d = _described(registry)
    assert d["switchable"] is False
    placed = _placed(registry)
    assert placed["placement"] == {"big_id": "sop", "sub_id": "sop.definition", "source": "taxonomy"}
    assert placed["movable"] is False  # kernel entries never move


# --------------------------------------------------------------------------- 3. disable

def test_disable_sop_slots_not_switchable(registry):
    d = _described(registry)
    assert d["kind"] == "K"
    assert d["switchable"] is False


# --------------------------------------------------------------------------- 4. provider

def test_provider_sop_slots_resolve_binds_explicit_and_implicit(module):
    provider = module(MODULE_ID).provider
    resolved = provider.resolve(_decls(), {"policy_docs": "kb_1", "order_query": "tool_2"}, binding_ids={"kb_1": "b_kb", "tool_2": "b_tool"})
    assert all(isinstance(r, ResolvedSlot) for r in resolved)
    got = {r.declaration.name: (r.resource_type, r.resource_id, r.binding_id) for r in resolved}
    assert got == {
        "policy_docs": ("knowledge_base", "kb_1", "b_kb"),
        "order_query": ("tool", "tool_2", "b_tool"),
        "tool:tool_fixed": ("tool", "tool_fixed", None),
    }


def test_provider_sop_slots_optional_unbound_is_skipped_required_raises(module):
    provider = module(MODULE_ID).provider
    resolved = provider.resolve(_decls(), {"policy_docs": "kb_1"})
    assert [r.declaration.name for r in resolved] == ["policy_docs", "tool:tool_fixed"]
    with pytest.raises(RequiredSlotMissing) as exc:
        provider.resolve(_decls(), {"order_query": "tool_2"})
    assert exc.value.details["slot"] == "policy_docs"


def test_provider_sop_slots_visibility_fence(module):
    provider = module(MODULE_ID).provider
    visible = {"knowledge_base": {"kb_1"}, "tool": {"tool_2"}}
    resolved = provider.resolve(_decls(), {"policy_docs": "kb_1", "order_query": "tool_2"}, visible_resources=visible)
    # implicit tool_fixed is not visible and not required -> silently dropped before the PEP ever sees it
    assert [r.resource_id for r in resolved] == ["kb_1", "tool_2"]
    with pytest.raises(SlotNotBound) as exc:
        provider.resolve(_decls(), {"policy_docs": "kb_hidden"}, visible_resources=visible)
    assert exc.value.details == {"slot": "policy_docs", "resource": "kb_hidden"}


def test_provider_sop_slots_same_sop_two_staffs(module):
    provider = module(MODULE_ID).provider
    a = provider.resolve(_decls(), {"policy_docs": "kb_a"})
    b = provider.resolve(_decls(), {"policy_docs": "kb_b"})
    assert a[0].resource_id == "kb_a" and b[0].resource_id == "kb_b"
    assert a[0].declaration == b[0].declaration


# --------------------------------------------------------------------------- 5. PEP

def test_pep_sop_slots_host_guard_denies_cross_tenant(guard, security_ctx, module):
    """No own policy actions (kernel), but the host it lives under maps sop.execute/v1 through the PEP."""

    assert module(MODULE_ID).manifest.policy_actions == ()
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    assert mapper.map("sop.execute/v1") == ("execute", "sop")
    with pytest.raises(KeyError):
        mapper.map("sop.slots.resolve/v1")  # unmapped operations never reach the PEP silently
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="sop", id="sop_x", tenant_id="t2")
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), "sop.execute/v1", foreign)
