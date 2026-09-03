"""Module tests for ``ingress.web`` (网页与接口入口): the web chat entry surfaced as an A ingress module."""

from __future__ import annotations

import re

import pytest

from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, ResourceRef
from staffdeck_harness.modules.kernel import WebIngressModule
from staffdeck_harness.modules.registry import SEMVER_RE, ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "ingress.web"
CJK = re.compile(r"[一-鿿]")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    return next(m for m in registry.describe() if m["module_id"] == module_id)


def _placement(registry, module_id: str = MODULE_ID) -> tuple[dict, dict, dict]:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return big, sub, m
    raise AssertionError(f"{module_id} not placed in taxonomy tree")


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_ingress_web(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind.value == "A"
    assert item.slot.value == "staff.ingress"
    assert m.attaches_to == (item.slot,)
    assert m.provides_operations == ("runtime.turn/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ("staff.use/v1",)
    assert m.hooks == ()
    assert SEMVER_RE.match(m.version)
    d = _described(registry)
    assert d["name"] == "网页与接口入口"
    assert d["summary"] and CJK.search(d["summary"])
    assert d["kind"] == "A" and d["slot"] == "staff.ingress" and d["enabled"] is True
    assert d["guarded"] is True and bool(d["policy_actions"]) is True
    assert d["source"] == "builtin"


def test_manifest_ingress_web_slot_guarded_by_builtin_registration(settings):
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    assert _described(reg)["guarded"] is True


# --------------------------------------------------------------------------- 2. placement

def test_placement_ingress_web(registry):
    big, sub, m = _placement(registry)
    assert big["id"] == "channel"
    assert sub["id"] == "channel.external"
    assert m["placement"] == {"big_id": "channel", "sub_id": "channel.external", "source": "taxonomy"}
    assert m["switchable"] is True
    assert m["movable"] is True
    assert "staff.ingress" in sub["slots"]


# --------------------------------------------------------------------------- 3. disable

def test_disable_ingress_web(settings):
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _described(reg)["enabled"] is False
    assert MODULE_ID not in {i.manifest.module_id for i in reg.providers(item.slot)}
    # the other ingress modules (and the active engine) keep runtime.turn/v1 provided
    assert reg.for_operation("runtime.turn/v1") is not None


# --------------------------------------------------------------------------- 4. provider

def test_provider_ingress_web_routes_match_legacy_chat_router(module):
    provider = module(MODULE_ID).provider
    assert isinstance(provider, WebIngressModule)
    assert provider.module_id == MODULE_ID
    assert provider.routes == ("/api/chat/turn", "/api/chat/stream")
    from app.api.chat import router

    legacy_paths = {r.path for r in router.routes}
    assert set(provider.routes) <= legacy_paths, f"declared routes not served by app.api.chat: {set(provider.routes) - legacy_paths}"
    turn = next(r for r in router.routes if r.path == "/api/chat/turn")
    assert "POST" in turn.methods


# --------------------------------------------------------------------------- 5. PEP

def test_pep_ingress_web_denies_cross_tenant_staff_use(guard, security_ctx):
    g = guard(MODULE_ID)
    assert DEFAULT_ACTION_MAP["staff.use/v1"] == ("use", "agent")
    foreign = ResourceRef(type="agent", id="a_x", tenant_id="t2", attributes={"owner_user_id": "u1", "is_overall": True, "status": "active"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), "staff.use/v1", foreign)
    assert exc.value.code == "PERMISSION_DENIED" and exc.value.details["operation"] == "staff.use/v1"
    own = ResourceRef(type="agent", id="a1", tenant_id="t1", attributes={"owner_user_id": "u1", "status": "active"})
    assert g.require(security_ctx(), "staff.use/v1", own).allowed is True
    # a same-tenant private staff owned by someone else is not usable from the web ingress either
    private = ResourceRef(type="agent", id="a2", tenant_id="t1", attributes={"owner_user_id": "someone", "status": "active"})
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), "staff.use/v1", private)
