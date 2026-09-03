"""staff.persona — kernel module rendering the employee's self-description (persona prompt)."""

from __future__ import annotations

import re

import pytest

from app.db.models import AgentProfile, PersonaConfig
from staffdeck_dsh.contracts.manifest import ModuleKind, SlotName
from staffdeck_dsh.modules.kernel import PersonaModule
from staffdeck_dsh.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_dsh.modules.taxonomy import tree

MODULE_ID = "staff.persona"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def test_manifest_staff_persona(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.KERNEL
    assert item.slot is SlotName.STAFF_MODEL_ROUTE
    assert m.attaches_to == (SlotName.STAFF_MODEL_ROUTE,)
    assert isinstance(item.provider, PersonaModule)
    assert item.provider.module_id == MODULE_ID

    d = _described(registry)
    assert d["kind"] == "K"
    assert d["slot"] == "staff.model_route"
    assert d["provides"] == []
    assert d["requires"] == []
    assert d["policy_actions"] == []
    assert d["hooks"] == []
    assert d["name"] == "员工人设"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["enabled"] is True
    assert d["source"] == "builtin"
    # no policy actions -> "guarded" only reflects the slot; the fixture guards every slot
    assert d["guarded"] is True
    assert d["guarded"] == (item.slot in registry._guarded_slots)


def test_placement_staff_persona(registry):
    d = _described(registry)
    assert d["switchable"] is False
    nodes = tree(registry.describe())
    staff = next(b for b in nodes if b["id"] == "staff")
    sub = next(s for s in staff["subs"] if s["id"] == "staff.profile")
    placed = [m for m in sub["modules"] if m["module_id"] == MODULE_ID]
    assert len(placed) == 1
    assert placed[0]["placement"] == {"big_id": "staff", "sub_id": "staff.profile", "source": "taxonomy"}
    assert placed[0]["movable"] is False
    # never shown under "unplaced"
    assert not any(b["id"] == "unplaced" and any(m["module_id"] == MODULE_ID for s in b["subs"] for m in s["modules"]) for b in nodes)


def test_disable_staff_persona(registry, settings):
    d = _described(registry)
    assert d["kind"] == "K" and d["switchable"] is False
    assert d["slot"] not in {"runtime.engine", "security.pep"}
    # The registry itself still honours a deployment-level disable (documented in kernel.py);
    # the admin API is the layer that refuses because switchable is False.
    settings.dsh_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is True  # kernel modules ignore disabled_modules
    assert any(i.manifest.module_id == MODULE_ID for i in reg.providers(SlotName.STAFF_MODEL_ROUTE))  # still served


def test_provider_staff_persona_renders_agent_identity(module, db):
    provider = module(MODULE_ID).provider
    agent = db.get(AgentProfile, "a1")
    out = provider.render(db, "t1", agent)
    assert isinstance(out, str) and out.strip()
    assert "Agent One" in out
    # delegates to the legacy identity prompt renderer
    from app.core.agent_loop import _agent_identity_prompt

    assert out == _agent_identity_prompt(agent)


def test_provider_staff_persona_overall_and_tenant_fallback(module, db):
    provider = module(MODULE_ID).provider
    # no agent and no PersonaConfig row -> None
    assert provider.render(db, "t1", None) is None
    # tenant-level PersonaConfig is the fallback for the overall agent / no agent
    db.add(PersonaConfig(tenant_id="t1", system_prompt="你是租户助理。"))
    db.commit()
    assert provider.render(db, "t1", None) == "你是租户助理。"
    overall = AgentProfile(id="ov", tenant_id="t1", name="Overall", status="active", is_overall=True, persona_prompt=None)
    assert provider.render(db, "t1", overall) == "你是租户助理。"
    overall_with_prompt = AgentProfile(id="ov2", tenant_id="t1", name="Overall2", status="active", is_overall=True, persona_prompt="总控人设")
    assert provider.render(db, "t1", overall_with_prompt) == "总控人设"


def test_events_or_pep_staff_persona_is_unguarded_but_slot_is_pep_bound(registry, guard, security_ctx):
    """staff.persona declares no policy actions; its slot is still guarded by the host that owns model routing."""

    from staffdeck_dsh.contracts.errors import PermissionDenied
    from staffdeck_dsh.contracts.security import ResourceRef

    d = _described(registry)
    assert d["policy_actions"] == [] and d["guarded"] is True
    # the sibling on the same slot (staff.model_route) is what carries the action; cross-tenant is denied there
    g = guard(MODULE_ID)
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), "model.use/v1", ResourceRef(type="model_config", id="m1", tenant_id="t2", attributes={"enabled": True}))
