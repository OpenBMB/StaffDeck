"""Per-module tests for ``security.oss_local`` — 开源版 · 本地权限 (kernel, slot security.pep).

Offline and deterministic: the provider builds the OSS_LOCAL SecurityProfile from
plain dataclasses; no network, no engine, no LLM.
"""

from __future__ import annotations

import re

import pytest

from app.db.models import User
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef, SecurityProfile
from staffdeck_harness.modules.builtin import OssLocalProfileModule
from staffdeck_harness.modules.registry import ModuleRegistry, SlotConflict, discover_and_install
from staffdeck_harness.modules.taxonomy import movable, tree
from staffdeck_harness.security.oss_local import LocalIdentity, LocalPep, LocalWorkload
from staffdeck_harness.security.profile import Guard, get_profile

from .conftest import FakeSettings

MODULE_ID = "security.oss_local"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")


def _describe(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1, f"{module_id} must be described exactly once"
    return hit[0]


# --------------------------------------------------------------------------- 1. manifest


def test_manifest_security_oss_local(registry, module, settings):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind.value == "K"
    assert item.slot is SlotName.SECURITY_PEP and m.attaches_to == (SlotName.SECURITY_PEP,)
    assert m.provides_operations == () and m.requires_operations == () and m.policy_actions == () and m.hooks == ()
    assert isinstance(item.provider, OssLocalProfileModule)
    assert item.enabled is True  # FakeSettings.security_profile == OSS_LOCAL → this is the active PEP module

    d = _describe(registry)
    assert d["name"] == "开源版权限（本地）"
    assert d["kind"] == "K" and d["slot"] == "security.pep" and d["enabled"] is True
    assert d["provides"] == [] and d["requires"] == [] and d["policy_actions"] == [] and d["hooks"] == []
    assert d["summary"] and CJK.search(d["summary"]) and d["summary"].endswith("。")
    assert SEMVER.match(d["version"])
    assert d["contract_version"] == "v1" and d["source"] == "builtin"

    # 'guarded' reflects whether the slot holds a Guard. The module declares no policy_actions, so a plain
    # built-in registry (where builtin.register never marks security.pep) describes it as unguarded and
    # still seals; the shared fixture marks every slot, hence guarded there.
    raw = discover_and_install(ModuleRegistry(), settings)
    raw.seal()
    plain = _describe(raw)
    assert plain["guarded"] is False and plain["guarded"] == bool(plain["policy_actions"])
    assert d["guarded"] is True


def test_manifest_security_oss_local_is_the_single_active_pep(registry):
    active = registry.providers(SlotName.SECURITY_PEP)
    assert [i.manifest.module_id for i in active] == [MODULE_ID]
    assert registry.provider(SlotName.SECURITY_PEP) is registry.get(MODULE_ID)
    # both PEP modules enabled at once is a seal-time conflict (exactly one active)
    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    reg.set_enabled("security.business_base", True)
    with pytest.raises(SlotConflict):
        reg.seal()


# --------------------------------------------------------------------------- 2. placement


def test_placement_security_oss_local(registry):
    nodes = tree(registry.describe())
    big = next(b for b in nodes if b["id"] == "permission")
    assert big["name"] == "统一权限" and big["pep"] is False and big["order"] == 8
    sub = next(s for s in big["subs"] if s["id"] == "permission.oss_local")
    assert sub["kind"] == "K" and sub["name"] == "开源版 · 本地权限"
    assert "app.security.permissions" in sub["legacy"]
    mods = [m for m in sub["modules"] if m["module_id"] == MODULE_ID]
    assert len(mods) == 1
    m = mods[0]
    assert m["placement"] == {"big_id": "permission", "sub_id": "permission.oss_local", "source": "taxonomy"}
    assert m["switchable"] is False and m["movable"] is False
    assert movable(m) is False  # kernel + fixed slot: operator overrides never re-parent it
    assert sub["enabled"] == 1 and sub["total"] == 1
    # never in the unplaced bucket, and only in its own sub
    for b in nodes:
        for s in b["subs"]:
            if (b["id"], s["id"]) != ("permission", "permission.oss_local"):
                assert MODULE_ID not in [x["module_id"] for x in s["modules"]]


def test_placement_security_oss_local_ignores_operator_override(registry):
    nodes = tree(registry.describe(), {MODULE_ID: "governance.trace"})
    big = next(b for b in nodes if b["id"] == "permission")
    sub = next(s for s in big["subs"] if s["id"] == "permission.oss_local")
    assert MODULE_ID in [x["module_id"] for x in sub["modules"]]


# --------------------------------------------------------------------------- 3. disable


def test_disable_security_oss_local_is_not_switchable(registry):
    d = _describe(registry)
    assert d["kind"] == "K" and d["switchable"] is False
    # admin API refuses both on the fixed-slot rule and on the switchable rule (api/admin.py validation)
    assert d["slot"] in {"runtime.engine", "security.pep"}
    assert not d["switchable"]


def test_disable_security_oss_local_selected_by_profile_not_by_toggle():
    """The PEP module is chosen through ``security_profile``; selecting BUSINESS_BASE disables this one."""

    class S(FakeSettings):
        security_profile = "BUSINESS_BASE"

    reg = discover_and_install(ModuleRegistry(), S())
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert reg.get("security.business_base").enabled is True
    assert reg.provider(SlotName.SECURITY_PEP).manifest.module_id == "security.business_base"


# --------------------------------------------------------------------------- 4. provider


def test_provider_security_oss_local_builds_profile(module, settings):
    provider = module(MODULE_ID).provider
    profile = provider.build(settings)
    assert isinstance(profile, SecurityProfile)
    assert profile.name == "OSS_LOCAL"
    assert isinstance(profile.pep, LocalPep) and profile.pep.profile == "OSS_LOCAL"
    assert isinstance(profile.identity, LocalIdentity)
    assert isinstance(profile.workload, LocalWorkload)
    # settings are irrelevant for the local profile: same shape with no settings at all
    assert provider.build(None).name == "OSS_LOCAL"


def test_provider_security_oss_local_pep_rules(module, settings, security_ctx):
    pep = module(MODULE_ID).provider.build(settings).pep
    member, admin = security_ctx(), security_ctx(principal_id="admin", tenant_role="admin")
    # deny by default: a knowledge base that is not bound to the acting staff
    unbound = ResourceRef(type="knowledge_base", id="kb1", tenant_id="t1")
    assert pep.authorize(member, MODULE_ID, "use", unbound).allowed is False
    # active binding + private-to-agent → use
    bound = ResourceRef(type="knowledge_base", id="kb1", tenant_id="t1", attributes={"binding_status": "active", "private_to_agent": True})
    assert pep.authorize(member, MODULE_ID, "use", bound).allowed is True
    assert pep.authorize(member, MODULE_ID, "manage", bound).allowed is False
    assert pep.authorize(admin, MODULE_ID, "manage", bound).allowed is True
    # staff: owner uses, stranger does not, gallery-published is visible to everyone in the tenant
    own = ResourceRef(type="agent", id="a1", tenant_id="t1", attributes={"owner_user_id": "u1"})
    other = ResourceRef(type="agent", id="a2", tenant_id="t1", attributes={"owner_user_id": "u9"})
    published = ResourceRef(type="agent", id="a3", tenant_id="t1", attributes={"owner_user_id": "u9", "published_to_gallery": True})
    assert pep.authorize(member, MODULE_ID, "use", own).allowed
    assert pep.authorize(member, MODULE_ID, "use", other).allowed is False
    assert pep.authorize(member, MODULE_ID, "use", published).allowed
    # deleted resources are unusable even for admins
    deleted = ResourceRef(type="agent", id="a4", tenant_id="t1", attributes={"status": "deleted"})
    assert pep.authorize(admin, MODULE_ID, "use", deleted).allowed is False
    # filter keeps the allowed subset in caller order
    assert [r.id for r in pep.filter(member, MODULE_ID, "use", [other, published, own])] == ["a3", "a1"]
    # every decision is stamped with the profile
    assert pep.authorize(member, MODULE_ID, "use", bound).source == "OSS_LOCAL"


def test_provider_security_oss_local_identity_and_workload(module, settings, db):
    profile = module(MODULE_ID).provider.build(settings)
    alice, admin = db.get(User, "u1"), db.get(User, "admin")
    ctx = profile.identity.from_user(alice)
    assert (ctx.principal_id, ctx.tenant_id, ctx.principal_type, ctx.tenant_role) == ("u1", "t1", "user", "member")
    assert ctx.username == "alice" and ctx.provider == "local" and ctx.is_admin is False
    assert profile.identity.from_user(admin).is_admin is True
    assert profile.identity.from_user(alice, channel="feishu").provider == "channel"
    assert profile.identity.from_user(alice, channel="web").provider == "local"
    svc = profile.identity.from_service("scheduler", "t1", workload={"run_id": "r1"})
    assert svc.principal_type == "service" and svc.tenant_role == "service" and svc.workload == {"run_id": "r1"}
    token = profile.workload.mint(ctx, audience="staffdeck-gateway", ttl_seconds=60)
    assert token["kind"] == "local" and token["tenant_id"] == "t1" and token["principal_id"] == "u1"
    assert token["audience"] == "staffdeck-gateway" and token["ttl_seconds"] == 60 and len(token["token_id"]) == 32
    assert token["token_id"] != profile.workload.mint(ctx, audience="x", ttl_seconds=1)["token_id"]


def test_provider_security_oss_local_selected_through_registry(settings):
    """``get_profile`` resolves the active security.pep module and asks *it* to build the profile."""

    profile = get_profile(settings)
    assert profile.name == "OSS_LOCAL" and isinstance(profile.pep, LocalPep)


# --------------------------------------------------------------------------- 5. PEP


def test_events_or_pep_security_oss_local_denies_cross_tenant(module, settings, security_ctx):
    """No policy_actions of its own: this module *is* the PEP every guarded action maps into."""

    profile = module(MODULE_ID).provider.build(settings)
    guard = Guard("knowledge.local", profile)
    assert guard.name == "OSS_LOCAL"
    foreign = ResourceRef(type="knowledge_base", id="kb9", tenant_id="t2", attributes={"binding_status": "active", "private_to_agent": True})
    with pytest.raises(PermissionDenied) as exc:
        guard.require(security_ctx(tenant_role="admin"), "knowledge.search/v1", foreign)
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.details["profile"] == "OSS_LOCAL" and exc.value.details["resource"] == "kb9"
    assert "tenant" in str(exc.value)
    # every operation in the default map resolves through this profile without KeyError
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    for op, (action, rtype) in DEFAULT_ACTION_MAP.items():
        assert mapper.map(op) == (action, rtype)
        d = profile.pep.authorize(security_ctx(), "test", action, ResourceRef(type=rtype, id="x", tenant_id="t2"))
        assert d.allowed is False and d.reason == "tenant boundary"
    with pytest.raises(KeyError):
        mapper.map("nope.op/v1")
