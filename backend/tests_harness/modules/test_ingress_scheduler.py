"""Module tests for ``ingress.scheduler`` (定时任务触发): scheduled tasks surfaced as an A ingress module."""

from __future__ import annotations

import re

import pytest

from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, ResourceRef
from staffdeck_harness.modules.kernel import SchedulerIngressModule
from staffdeck_harness.modules.registry import SEMVER_RE, ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "ingress.scheduler"
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

def test_manifest_ingress_scheduler(registry, module):
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
    assert d["name"] == "定时任务触发"
    assert d["summary"] and CJK.search(d["summary"])
    assert d["kind"] == "A" and d["slot"] == "staff.ingress" and d["enabled"] is True
    assert d["guarded"] is True and bool(d["policy_actions"]) is True
    assert d["source"] == "builtin"


# --------------------------------------------------------------------------- 2. placement

def test_placement_ingress_scheduler(registry):
    big, sub, m = _placement(registry)
    assert big["id"] == "channel"
    assert sub["id"] == "channel.scheduler"
    # curated taxonomy wins over the staff.ingress slot default (which would be channel.external)
    assert m["placement"] == {"big_id": "channel", "sub_id": "channel.scheduler", "source": "taxonomy"}
    assert m["switchable"] is True
    assert m["movable"] is True
    assert [x["module_id"] for x in sub["modules"]] == [MODULE_ID]


# --------------------------------------------------------------------------- 3. disable

def test_disable_ingress_scheduler(settings):
    """A deployment without scheduled tasks disables the module; the tree still shows it (enabled=False)."""

    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _described(reg)["enabled"] is False
    assert MODULE_ID not in {i.manifest.module_id for i in reg.providers(item.slot)}
    _big, sub, m = _placement(reg)
    assert sub["id"] == "channel.scheduler" and sub["enabled"] == 0 and sub["total"] == 1 and m["enabled"] is False
    assert reg.for_operation("runtime.turn/v1") is not None


# --------------------------------------------------------------------------- 4. provider

def test_provider_ingress_scheduler_dispatch_resolves_legacy_service(module):
    provider = module(MODULE_ID).provider
    assert isinstance(provider, SchedulerIngressModule)
    assert provider.module_id == MODULE_ID
    from app.scheduled_tasks import service as legacy_service

    service = provider.dispatch()
    assert service is legacy_service
    for name in ("due_scheduled_tasks", "execute_scheduled_task", "start_scheduled_task_async", "compute_next_run_at"):
        assert callable(getattr(service, name)), name
    # dispatch() is a pure accessor: extra args are ignored and the same module object comes back
    assert provider.dispatch("ignored", key="value") is legacy_service


def test_provider_ingress_scheduler_due_tasks_empty_offline(module, db):
    service = module(MODULE_ID).provider.dispatch()
    assert service.due_scheduled_tasks(db) == []


# --------------------------------------------------------------------------- 5. PEP

def test_pep_ingress_scheduler_denies_cross_tenant_staff_use(guard, security_ctx):
    g = guard(MODULE_ID)
    assert DEFAULT_ACTION_MAP["staff.use/v1"] == ("use", "agent")
    foreign = ResourceRef(type="agent", id="a_x", tenant_id="t2", attributes={"owner_user_id": "u1", "is_overall": True, "status": "active"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), "staff.use/v1", foreign)
    assert exc.value.code == "PERMISSION_DENIED" and exc.value.details["operation"] == "staff.use/v1"
    # the scheduler runs as a service principal: same-tenant active staff allowed, cross-tenant / deleted denied
    svc = security_ctx(principal_id="scheduler", principal_type="service", tenant_role="service")
    own = ResourceRef(type="agent", id="a1", tenant_id="t1", attributes={"owner_user_id": "someone", "status": "active"})
    assert g.require(svc, "staff.use/v1", own).allowed is True
    with pytest.raises(PermissionDenied):
        g.require(svc, "staff.use/v1", foreign)
    with pytest.raises(PermissionDenied):
        g.require(svc, "staff.use/v1", ResourceRef(type="agent", id="a3", tenant_id="t1", attributes={"status": "deleted"}))
