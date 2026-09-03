"""staff.model_route — kernel module deciding which model config each role of an employee uses."""

from __future__ import annotations

import re

import pytest

from app.db.models import AgentModelBinding, ModelConfig, utc_now
from app.security.encryption import encrypt_secret
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.modules.kernel import ModelRouteModule
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "staff.model_route"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def test_manifest_staff_model_route(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.KERNEL
    assert item.slot is SlotName.STAFF_MODEL_ROUTE
    assert m.attaches_to == (SlotName.STAFF_MODEL_ROUTE,)
    assert isinstance(item.provider, ModelRouteModule)
    assert item.provider.module_id == MODULE_ID

    d = _described(registry)
    assert d["kind"] == "K"
    assert d["slot"] == "staff.model_route"
    assert d["provides"] == ["model.use/v1"]
    assert d["requires"] == []
    assert d["policy_actions"] == ["model.use/v1"]
    assert d["hooks"] == []
    assert d["name"] == "模型分配"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["enabled"] is True
    assert d["source"] == "builtin"
    assert d["guarded"] is True  # declares policy actions -> must sit under a PEP-bound slot
    # the registry resolves model.use/v1 to this module
    assert registry.for_operation("model.use/v1") is item


def test_manifest_staff_model_route_seal_refuses_unguarded_slot(settings):
    """A module with policy_actions cannot boot unless its slot has a Guard-holding host (PepBindingMissing)."""

    from staffdeck_harness.contracts.errors import PepBindingMissing

    reg = discover_and_install(ModuleRegistry(), settings)
    # builtin.register marks the slot guarded; emulate a host that forgot by clearing it
    reg._guarded_slots.discard(SlotName.STAFF_MODEL_ROUTE)
    for slot in SlotName:
        if slot is not SlotName.STAFF_MODEL_ROUTE:
            reg.mark_guarded(slot)
    with pytest.raises(PepBindingMissing):
        reg.seal()


def test_placement_staff_model_route(registry):
    d = _described(registry)
    assert d["switchable"] is False
    nodes = tree(registry.describe())
    staff = next(b for b in nodes if b["id"] == "staff")
    sub = next(s for s in staff["subs"] if s["id"] == "staff.profile")
    placed = [m for m in sub["modules"] if m["module_id"] == MODULE_ID]
    assert len(placed) == 1
    assert placed[0]["placement"] == {"big_id": "staff", "sub_id": "staff.profile", "source": "taxonomy"}
    assert placed[0]["movable"] is False
    # staff.profile holds both persona and model_route (the slot's two kernel entries)
    assert {m["module_id"] for m in sub["modules"]} >= {"staff.persona", MODULE_ID}
    assert sub["slots"] == ["staff.model_route"]


def test_disable_staff_model_route(registry, settings):
    d = _described(registry)
    assert d["kind"] == "K" and d["switchable"] is False
    assert d["slot"] not in {"runtime.engine", "security.pep"}
    # registry-level disable still works (deployment config); admin API refuses via switchable=False
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is True  # kernel modules ignore disabled_modules
    assert reg.for_operation("model.use/v1") is not None  # still resolvable


def test_provider_staff_model_route_default_and_role_bindings(module, db):
    provider = module(MODULE_ID).provider
    # tenant default model wins when the agent has no explicit binding
    assert provider.route(db, "t1", "a1") == {"default": "m1"}
    assert provider.route(db, "t1", None) == {"default": "m1"}
    # unknown tenant -> nothing configured
    assert provider.route(db, "t-none", "a1") == {}

    db.add(ModelConfig(id="m2", tenant_id="t1", name="Fast", provider="openai_compatible", base_url="http://x/v1", api_key_encrypted=encrypt_secret("k"), model="fast", is_default=False, enabled=True, created_at=utc_now(), updated_at=utc_now()))
    db.add(AgentModelBinding(tenant_id="t1", agent_id="a1", role="planner", model_config_id="m2"))
    db.commit()
    route = provider.route(db, "t1", "a1")
    assert route == {"default": "m1", "planner": "m2"}

    # an explicit default binding overrides the tenant default
    db.add(AgentModelBinding(tenant_id="t1", agent_id="a1", role="default", model_config_id="m2"))
    db.commit()
    assert provider.route(db, "t1", "a1")["default"] == "m2"
    # other agents are unaffected
    assert provider.route(db, "t1", "a-other") == {"default": "m1"}

    from staffdeck_harness.composition.projection import agent_model_route

    assert dict(provider.route(db, "t1", "a1")) == agent_model_route(db, "t1", "a1")


def test_events_or_pep_staff_model_route_denies_cross_tenant(registry, guard, security_ctx):
    action, rtype = PolicyActionMapper(DEFAULT_ACTION_MAP).map("model.use/v1")
    assert (action, rtype) == ("use", "model_config")
    for op in _described(registry)["policy_actions"]:
        PolicyActionMapper(DEFAULT_ACTION_MAP).map(op)  # every declared action is known to the mapper

    g = guard(MODULE_ID)
    assert g.name == "OSS_LOCAL"
    foreign = ResourceRef(type="model_config", id="m1", tenant_id="t2", attributes={"enabled": True})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), "model.use/v1", foreign)
    assert "tenant" in str(exc.value)
    assert exc.value.details["operation"] == "model.use/v1"
    # same tenant, enabled model -> allowed for a plain member; disabled model -> denied
    own = ResourceRef(type="model_config", id="m1", tenant_id="t1", attributes={"enabled": True})
    assert g.require(security_ctx(), "model.use/v1", own).allowed
    disabled = ResourceRef(type="model_config", id="m3", tenant_id="t1", attributes={"enabled": False})
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), "model.use/v1", disabled)
