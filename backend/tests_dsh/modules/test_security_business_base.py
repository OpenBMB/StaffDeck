"""Per-module tests for ``security.business_base`` — 企业版 · 统一权限中心 (kernel, slot security.pep).

Offline and deterministic: the permission centre is an ``httpx.MockTransport``
handler; the profile must fail closed whenever that handler misbehaves.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from app.db.models import User
from staffdeck_dsh.contracts.errors import AuthorizationUnavailable, PermissionDenied
from staffdeck_dsh.contracts.manifest import SlotName
from staffdeck_dsh.contracts.security import DEFAULT_ACTION_MAP, ResourceRef, SecurityProfile
from staffdeck_dsh.modules.builtin import BusinessBaseProfileModule
from staffdeck_dsh.modules.registry import ModuleRegistry, SlotConflict, discover_and_install
from staffdeck_dsh.modules.taxonomy import movable, tree
from staffdeck_dsh.security.business_base import BaseAuthzClient, BaseIdentity, BasePep, BaseWorkload
from staffdeck_dsh.security.profile import Guard, get_profile

from .conftest import FakeSettings

MODULE_ID = "security.business_base"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")


class BusinessSettings(FakeSettings):
    """FakeSettings with the permission centre configured (no identity service)."""

    security_profile = "BUSINESS_BASE"
    base_authz_url = "http://base"
    base_authz_decision_token = "tok"
    base_authz_pending_timeout_seconds = 0.01


class BusinessSettingsWithIdentity(BusinessSettings):
    base_identity_internal_url = "http://identity"
    base_identity_runtime_client_id = "rt"
    base_identity_runtime_client_secret = "s"


def _describe(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1, f"{module_id} must be described exactly once"
    return hit[0]


def _mock(profile: SecurityProfile, handler) -> list[dict]:
    """Swap the profile's authz HTTP client for a MockTransport; returns the captured request bodies."""

    seen: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append({"path": request.url.path, "token": request.headers.get("X-Base-Service-Token"), "body": body})
        return handler(request, body)

    profile.pep.client.client = httpx.Client(transport=httpx.MockTransport(_handler))
    return seen


def _allow(request: httpx.Request, body: dict) -> httpx.Response:
    return httpx.Response(200, json={"request_id": body["request_id"], "allowed": True, "reason": "ok", "decision_id": "d1", "revision": "7"})


# --------------------------------------------------------------------------- 1. manifest


def test_manifest_security_business_base(registry, module, settings):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind.value == "K"
    assert item.slot is SlotName.SECURITY_PEP and m.attaches_to == (SlotName.SECURITY_PEP,)
    assert m.provides_operations == () and m.requires_operations == () and m.policy_actions == () and m.hooks == ()
    assert isinstance(item.provider, BusinessBaseProfileModule)
    assert item.enabled is False  # FakeSettings.security_profile == OSS_LOCAL → installed, inactive

    d = _describe(registry)
    assert d["name"] == "企业版权限（统一权限中心）"
    assert d["kind"] == "K" and d["slot"] == "security.pep" and d["enabled"] is False
    assert d["provides"] == [] and d["requires"] == [] and d["policy_actions"] == [] and d["hooks"] == []
    assert d["summary"] and CJK.search(d["summary"]) and d["summary"].endswith("。")
    assert "拒绝" in d["summary"]  # user-facing copy states the fail-closed behaviour
    assert SEMVER.match(d["version"])
    assert d["contract_version"] == "v1" and d["source"] == "builtin"

    # no policy_actions → a plain built-in registry leaves security.pep unguarded and still seals
    raw = discover_and_install(ModuleRegistry(), settings)
    raw.seal()
    plain = _describe(raw)
    assert plain["guarded"] is False and plain["guarded"] == bool(plain["policy_actions"])
    assert d["guarded"] is True  # the shared fixture marks every slot


def test_manifest_security_business_base_is_active_only_under_its_profile():
    reg = discover_and_install(ModuleRegistry(), BusinessSettings())
    reg.seal()
    assert [i.manifest.module_id for i in reg.providers(SlotName.SECURITY_PEP)] == [MODULE_ID]
    assert reg.get("security.oss_local").enabled is False
    # the selector is case-insensitive
    class Lower(BusinessSettings):
        security_profile = "business_base"

    reg2 = discover_and_install(ModuleRegistry(), Lower())
    assert reg2.get(MODULE_ID).enabled is True
    # two active PEP modules never seal
    reg3 = discover_and_install(ModuleRegistry(), BusinessSettings())
    reg3.set_enabled("security.oss_local", True)
    with pytest.raises(SlotConflict):
        reg3.seal()


# --------------------------------------------------------------------------- 2. placement


def test_placement_security_business_base(registry):
    nodes = tree(registry.describe())
    big = next(b for b in nodes if b["id"] == "permission")
    assert big["name"] == "统一权限" and big["pep"] is False and big["order"] == 8
    sub = next(s for s in big["subs"] if s["id"] == "permission.business_base")
    assert sub["kind"] == "K" and sub["name"] == "企业版 · 统一权限中心"
    mods = [m for m in sub["modules"] if m["module_id"] == MODULE_ID]
    assert len(mods) == 1
    m = mods[0]
    assert m["placement"] == {"big_id": "permission", "sub_id": "permission.business_base", "source": "taxonomy"}
    assert m["switchable"] is False and m["movable"] is False and movable(m) is False
    assert sub["total"] == 1 and sub["enabled"] == 0  # installed, inactive under OSS_LOCAL
    for b in nodes:
        for s in b["subs"]:
            if (b["id"], s["id"]) != ("permission", "permission.business_base"):
                assert MODULE_ID not in [x["module_id"] for x in s["modules"]]
    # operator overrides never re-parent a fixed-slot kernel module
    nodes2 = tree(registry.describe(), {MODULE_ID: "governance.trace"})
    sub2 = next(s for b in nodes2 if b["id"] == "permission" for s in b["subs"] if s["id"] == "permission.business_base")
    assert MODULE_ID in [x["module_id"] for x in sub2["modules"]]


# --------------------------------------------------------------------------- 3. disable


def test_disable_security_business_base_is_not_switchable(registry):
    d = _describe(registry)
    assert d["kind"] == "K" and d["switchable"] is False
    # api/admin.py refuses: fixed slot ("通过选择引擎 / 权限模式切换") and non-switchable ("平台核心组成部分")
    assert d["slot"] in {"runtime.engine", "security.pep"}
    assert not d["switchable"]


def test_disable_security_business_base_selected_by_profile_not_by_toggle():
    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    reg.seal()
    assert reg.get(MODULE_ID).enabled is False
    assert reg.provider(SlotName.SECURITY_PEP).manifest.module_id == "security.oss_local"


# --------------------------------------------------------------------------- 4. provider


def test_provider_security_business_base_requires_url_and_token(module):
    provider = module(MODULE_ID).provider

    class NoUrl(BusinessSettings):
        base_authz_url = ""

    class NoToken(BusinessSettings):
        base_authz_decision_token = ""

    for bad in (NoUrl(), NoToken()):
        with pytest.raises(ValueError, match="base_authz_url and base_authz_decision_token"):
            provider.build(bad)


def test_provider_security_business_base_builds_profile(module):
    provider = module(MODULE_ID).provider
    profile = provider.build(BusinessSettings())
    assert isinstance(profile, SecurityProfile) and profile.name == "BUSINESS_BASE"
    assert isinstance(profile.pep, BasePep) and profile.pep.profile == "BUSINESS_BASE"
    assert isinstance(profile.identity, BaseIdentity)
    client = profile.pep.client
    assert isinstance(client, BaseAuthzClient)
    assert client.base_url == "http://base" and client.config.decision_token == "tok"
    assert client.config.pending_timeout_seconds == 0.01
    # no identity service configured → workload exchange is explicit, never a silent local token
    with pytest.raises(RuntimeError, match="not configured"):
        profile.workload.mint(BaseIdentity().from_service("svc", "t1"), audience="staffdeck-gateway", ttl_seconds=60)
    # identity service configured → Base workload flow bound to the same authz client
    with_identity = provider.build(BusinessSettingsWithIdentity())
    assert isinstance(with_identity.workload, BaseWorkload)
    assert with_identity.workload.authz is with_identity.pep.client
    assert with_identity.workload.config.client_id == "rt" and with_identity.workload.config.audience == "staffdeck-gateway"


def test_provider_security_business_base_delegates_shared_resources_to_base(module, security_ctx):
    profile = module(MODULE_ID).provider.build(BusinessSettings())
    seen = _mock(profile, _allow)
    kb = ResourceRef(type="knowledge_base", id="kb:1", tenant_id="t1")
    d = profile.pep.authorize(security_ctx(), MODULE_ID, "use", kb)
    assert d.allowed is True and d.source == "BUSINESS_BASE" and d.decision_id == "d1" and d.revision == "7"
    assert len(seen) == 1
    req = seen[0]
    assert req["path"] == "/internal/v1/authz/check" and req["token"] == "tok"
    body = req["body"]
    assert body["principal"] == {"type": "user", "id": "u1", "tenant_id": "t1"}
    assert body["action"] == "retrieve"  # knowledge_base 'use' is a retrieval in Base's vocabulary
    assert body["resource"] == {"type": "knowledge_base", "id": "kb_3a1", "tenant_id": "t1"}  # ':' hex-encoded, injective
    assert body["consistency"] == "minimize_latency" and len(body["request_id"]) == 32
    # mcp_server collapses onto Base's 'tool' type
    profile.pep.authorize(security_ctx(), MODULE_ID, "use", ResourceRef(type="mcp_server", id="m1", tenant_id="t1"))
    assert seen[-1]["body"]["resource"]["type"] == "tool" and seen[-1]["body"]["action"] == "use"


def test_provider_security_business_base_keeps_runtime_resources_local(module, security_ctx):
    profile = module(MODULE_ID).provider.build(BusinessSettings())
    seen = _mock(profile, lambda request, body: httpx.Response(500))
    own = ResourceRef(type="session", id="s1", tenant_id="t1", attributes={"user_id": "u1"})
    other = ResourceRef(type="session", id="s2", tenant_id="t1", attributes={"user_id": "u9"})
    ok = profile.pep.authorize(security_ctx(), MODULE_ID, "write", own)
    no = profile.pep.authorize(security_ctx(), MODULE_ID, "write", other)
    assert ok.allowed is True and ok.source == "BUSINESS_BASE" and ok.reason.startswith("local policy:")
    assert no.allowed is False and no.reason == "local policy: not the session owner"
    assert seen == []  # Base was never consulted for a runtime-internal resource


def test_provider_security_business_base_fails_closed(module, security_ctx):
    provider = module(MODULE_ID).provider
    agent = ResourceRef(type="agent", id="a1", tenant_id="t1")
    ctx = security_ctx()

    down = provider.build(BusinessSettings())
    calls = _mock(down, lambda request, body: httpx.Response(503))
    d = down.pep.authorize(ctx, MODULE_ID, "use", agent)
    assert d.allowed is False and d.reason.startswith("authorization unavailable")
    assert len(calls) == 2  # one retry on 5xx, then deny

    bad_token = provider.build(BusinessSettings())
    _mock(bad_token, lambda request, body: httpx.Response(401))
    d = bad_token.pep.authorize(ctx, MODULE_ID, "use", agent)
    assert d.allowed is False and "misconfigured" in d.reason and "401" in d.reason

    garbage = provider.build(BusinessSettings())
    _mock(garbage, lambda request, body: httpx.Response(200, content=b"not json"))
    assert garbage.pep.authorize(ctx, MODULE_ID, "use", agent).reason == "authorization unavailable: non-JSON body"

    mismatch = provider.build(BusinessSettings())
    _mock(mismatch, lambda request, body: httpx.Response(200, json={"request_id": "other", "allowed": True}))
    assert mismatch.pep.authorize(ctx, MODULE_ID, "use", agent).reason == "authorization unavailable: contract error"

    pending = provider.build(BusinessSettings())
    pending.pep._sleep = lambda _s: None
    ticks = iter(range(0, 10_000))
    pending.pep._clock = lambda: float(next(ticks))  # each tick is a second → deadline passes on the second look
    n = _mock(pending, lambda request, body: httpx.Response(200, json={"request_id": body["request_id"], "allowed": False, "reason": "authorization_pending"}))
    d = pending.pep.authorize(ctx, MODULE_ID, "use", agent)
    assert d.allowed is False and d.pending is True and d.reason == "authorization pending"
    assert len(n) >= 1

    denied = provider.build(BusinessSettings())
    _mock(denied, lambda request, body: httpx.Response(200, json={"request_id": body["request_id"], "allowed": False, "reason": "no relation"}))
    d = denied.pep.authorize(ctx, MODULE_ID, "use", agent)
    assert d.allowed is False and d.reason == "no relation" and d.pending is False


def test_provider_security_business_base_filter_uses_batch_check(module, security_ctx):
    profile = module(MODULE_ID).provider.build(BusinessSettings())

    def handler(request: httpx.Request, body: dict) -> httpx.Response:
        assert request.url.path == "/internal/v1/authz/batch-check"
        decisions = [{"request_id": r["request_id"], "allowed": r["resource"]["id"] == "ok"} for r in body["requests"]]
        return httpx.Response(200, json={"decisions": decisions})

    seen = _mock(profile, handler)
    ok = ResourceRef(type="tool", id="ok", tenant_id="t1")
    bad = ResourceRef(type="tool", id="bad", tenant_id="t1")
    foreign = ResourceRef(type="tool", id="ok", tenant_id="t2")
    local = ResourceRef(type="session", id="s1", tenant_id="t1", attributes={"user_id": "u1"})
    out = profile.pep.filter(security_ctx(), MODULE_ID, "use", [bad, local, foreign, ok])
    assert out == [local, ok]  # caller order kept; foreign tenant dropped before any wire call
    assert len(seen) == 1 and [r["resource"]["id"] for r in seen[0]["body"]["requests"]] == ["bad", "ok"]
    # an unanswerable batch grants nothing
    _mock(profile, lambda request, body: httpx.Response(503))
    assert profile.pep.filter(security_ctx(), MODULE_ID, "use", [ok, bad]) == []


def test_provider_security_business_base_identity(module, db):
    profile = module(MODULE_ID).provider.build(BusinessSettings())
    alice, admin = db.get(User, "u1"), db.get(User, "admin")
    ctx = profile.identity.from_user(alice)
    assert (ctx.principal_id, ctx.tenant_id, ctx.tenant_role, ctx.provider) == ("u1", "t1", "member", "base_identity")
    assert profile.identity.from_user(admin).is_admin is True
    assert profile.identity.from_user(alice, channel="feishu").channel == "feishu"
    svc = profile.identity.from_service("scheduler", "t1")
    assert svc.principal_type == "workload" and svc.tenant_role == "service" and svc.provider == "base_identity"


def test_provider_security_business_base_selected_through_registry():
    """``get_profile`` resolves the active security.pep module (this one) and lets it build the profile."""

    profile = get_profile(BusinessSettings())
    assert profile.name == "BUSINESS_BASE" and isinstance(profile.pep, BasePep)
    assert profile.pep.client.base_url == "http://base"


# --------------------------------------------------------------------------- 5. PEP


def test_events_or_pep_security_business_base_denies_cross_tenant(module, security_ctx):
    """No policy_actions of its own: this module *is* the PEP guarded actions map into; cross-tenant
    is refused locally before the permission centre is ever asked."""

    profile = module(MODULE_ID).provider.build(BusinessSettings())
    seen = _mock(profile, _allow)
    guard = Guard("knowledge.local", profile)
    assert guard.name == "BUSINESS_BASE"
    foreign = ResourceRef(type="knowledge_base", id="kb9", tenant_id="t2")
    with pytest.raises(PermissionDenied) as exc:
        guard.require(security_ctx(tenant_role="admin"), "knowledge.search/v1", foreign)
    assert exc.value.code == "PERMISSION_DENIED" and exc.value.details["profile"] == "BUSINESS_BASE"
    assert seen == []
    for op, (action, rtype) in DEFAULT_ACTION_MAP.items():
        d = profile.pep.authorize(security_ctx(), "test", action, ResourceRef(type=rtype, id="x", tenant_id="t2"))
        assert d.allowed is False and d.reason == "tenant boundary" and d.source == "BUSINESS_BASE"
    assert seen == []
    # same tenant, permission centre down → typed AuthorizationUnavailable (fail closed, never allow)
    _mock(profile, lambda request, body: httpx.Response(503))
    with pytest.raises(AuthorizationUnavailable) as unavailable:
        guard.require(security_ctx(), "knowledge.search/v1", ResourceRef(type="knowledge_base", id="kb1", tenant_id="t1"))
    assert unavailable.value.code == "AUTHORIZATION_UNAVAILABLE"
