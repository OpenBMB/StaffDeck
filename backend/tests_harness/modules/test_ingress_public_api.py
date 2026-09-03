"""Module tests for ``ingress.public_api`` (开放接口入口): the /api/v1 public API surfaced as an A ingress module."""

from __future__ import annotations

import re

import pytest

from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, ResourceRef
from staffdeck_harness.modules.kernel import PublicApiIngressModule
from staffdeck_harness.modules.registry import SEMVER_RE, ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "ingress.public_api"
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

def test_manifest_ingress_public_api(registry, module):
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
    assert d["name"] == "开放接口入口"
    assert d["summary"] and CJK.search(d["summary"])
    assert d["kind"] == "A" and d["slot"] == "staff.ingress" and d["enabled"] is True
    assert d["guarded"] is True and bool(d["policy_actions"]) is True
    assert d["source"] == "builtin"


# --------------------------------------------------------------------------- 2. placement

def test_placement_ingress_public_api(registry):
    big, sub, m = _placement(registry)
    assert big["id"] == "channel"
    assert sub["id"] == "channel.external"
    assert m["placement"] == {"big_id": "channel", "sub_id": "channel.external", "source": "taxonomy"}
    assert m["switchable"] is True
    assert m["movable"] is True
    # shares the sub-module with ingress.web
    assert {x["module_id"] for x in sub["modules"]} >= {"ingress.web", "ingress.public_api"}


# --------------------------------------------------------------------------- 3. disable

def test_disable_ingress_public_api(settings):
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _described(reg)["enabled"] is False
    assert MODULE_ID not in {i.manifest.module_id for i in reg.providers(item.slot)}
    assert reg.get("ingress.web").enabled is True
    assert reg.for_operation("runtime.turn/v1") is not None


# --------------------------------------------------------------------------- 4. provider

def test_provider_ingress_public_api_routes_match_public_api_app(module):
    provider = module(MODULE_ID).provider
    assert isinstance(provider, PublicApiIngressModule)
    assert provider.module_id == MODULE_ID
    assert provider.routes == ("/api/v1/*",)
    # the wildcard covers the sub-application mounted at /api/v1 (app.main mounts create_public_api_app())
    from app.public_api.app import create_public_api_app

    sub_app = create_public_api_app()
    paths = {getattr(r, "path", "") for r in sub_app.routes}
    assert "/agents/{agent_id}/runs" in paths, sorted(paths)
    assert all(not p.startswith("/api/v1") for p in paths)  # sub-app paths are relative to the mount


# --------------------------------------------------------------------------- 5. PEP

def test_pep_ingress_public_api_denies_cross_tenant_staff_use(guard, security_ctx):
    g = guard(MODULE_ID)
    assert DEFAULT_ACTION_MAP["staff.use/v1"] == ("use", "agent")
    foreign = ResourceRef(type="agent", id="a_x", tenant_id="t2", attributes={"owner_user_id": "u1", "is_overall": True, "status": "active"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), "staff.use/v1", foreign)
    assert exc.value.code == "PERMISSION_DENIED" and exc.value.details["operation"] == "staff.use/v1"
    # API keys act as a service principal: same-tenant use is allowed, cross-tenant still denied
    svc = security_ctx(principal_id="apikey_1", principal_type="service", tenant_role="service")
    own = ResourceRef(type="agent", id="a1", tenant_id="t1", attributes={"owner_user_id": "someone", "status": "active"})
    assert g.require(svc, "staff.use/v1", own).allowed is True
    with pytest.raises(PermissionDenied):
        g.require(svc, "staff.use/v1", foreign)
