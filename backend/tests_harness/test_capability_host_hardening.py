"""Tests for the CapabilityHost hardening from the Harness v3 review.

Each test here pins one behaviour the review found missing:

1. The PEP is a *host* obligation — a provider that never calls ``guard.require`` still cannot
   bypass the policy (``CapabilityHost._pep`` runs before ``_dispatch``).
2. ``pre_tool`` / ``post_tool`` hooks actually run around every capability call, and a
   ``deny`` from ``pre_tool`` refuses the call before the ledger records anything.
3. ``finish_task`` closes the slot: the *next* tool call in the same turn is fenced.
4. Provider pinning: a grant's ``provider_module_id`` is honoured and a pinned-but-missing
   provider fails closed rather than falling through to the first enabled module.
5. ``idempotency.enabled=false`` on a POST tool disables replay/dedupe but keeps the call
   side-effecting (ambiguous failure → ``outcome_unknown``).
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import AgentProfile, ModelConfig, Tenant, Tool, User, utc_now
from app.security.auth import hash_password
from app.security.encryption import encrypt_secret
from staffdeck_harness.capabilities.host import ActivationSlot, CapabilityHost, LifecycleFence
from staffdeck_harness.composition.compiler import CapabilityGrant, CompositionCompiler
from staffdeck_harness.composition.staff import CapabilityBindingView, SessionPolicy, StaffComposition
from staffdeck_harness.contracts.hooks import HookDecision
from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation, ModuleResult
from staffdeck_harness.contracts.manifest import ModuleKind, ModuleManifest, SlotName
from staffdeck_harness.contracts.security import ResourceRef, SecurityContext
from staffdeck_harness.modules import registry as registry_mod
from staffdeck_harness.modules.registry import ModuleRegistry
from staffdeck_harness.security.oss_local import build_oss_local_profile
from staffdeck_harness.security.profile import Guard
from app.agents.branching import ensure_private_resource_binding


# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Tenant(id="t1", name="T1"))
        s.add(User(id="u1", tenant_id="t1", username="alice", role="member", password_hash=hash_password("x")))
        s.add(AgentProfile(id="a1", tenant_id="t1", name="Agent One", status="active", metadata_json={"owner_user_id": "u1"}))
        s.add(ModelConfig(id="m1", tenant_id="t1", name="GLM", provider="openai_compatible", base_url="http://x/v1", api_key_encrypted=encrypt_secret("k"), model="glm", is_default=True, enabled=True, created_at=utc_now(), updated_at=utc_now()))
        s.commit()
        yield s


def _ref(t, i, **attrs):
    return ResourceRef(type=t, id=i, tenant_id="t1", attributes=attrs)


def _tool(db, tool_id: str, *, method: str = "POST", idempotency: dict | None = None) -> Tool:
    cfg = {"idempotency": idempotency} if idempotency is not None else {}
    row = Tool(
        id=tool_id, tenant_id="t1", name=f"name_{tool_id}", display_name="订单查询", description="按订单号查询", tool_type="http", method=method,
        url="http://tools.invalid/query", input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}}, enabled=True, config_json=cfg,
    )
    db.add(row)
    db.commit()
    return row


def _staff(caps=()):
    return StaffComposition(
        tenant_id="t1", staff_id="a1", name="A", is_overall=False, status="active", persona="你是A",
        model_route={"default": "m1"}, session_policy=SessionPolicy(), capabilities=tuple(caps), sops=(),
        channels=(), team=None, interactions=("sop_adapter",), ref=_ref("agent", "a1", owner_user_id="u1"),
    )


def _cap(t, i, metadata=None):
    return CapabilityBindingView(resource_type=t, resource_id=i, binding_id=f"b_{i}", ref=_ref(t, i), name=i, metadata=dict(metadata or {}))


def _ctx(**kw) -> InvocationContext:
    base = dict(tenant_id="t1", agent_id="a1", user_id="u1", session_id="s1", turn_id="turn1", channel="web", task_frame_id="tf1", step_id=None, run_id="run1", snapshot_id="snap", trace_id="hcall_1")
    base.update(kw)
    return InvocationContext(**base)


class _RecordingProvider:
    """A provider that does NOT call ``guard.require`` — the exact bypass the review described."""

    def __init__(self):
        self.calls: list[str] = []

    def invoke(self, host, inv):
        self.calls.append(inv.operation)
        return ModuleResult.ok({"echo": dict(inv.arguments)})


def _install_registry(monkeypatch, provider, *, module_id="tool.fake", ops=("tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"), extra=()):
    reg = ModuleRegistry()
    reg.install(ModuleManifest(module_id=module_id, name="fake", version="1.0.0", kind=ModuleKind.CODE, contract_version="v1", attaches_to=(SlotName.STAFF_CAPABILITY,), provides_operations=tuple(ops), policy_actions=tuple(ops)), provider, slot=SlotName.STAFF_CAPABILITY)
    for mid, prov, pops in extra:
        reg.install(ModuleManifest(module_id=mid, name=mid, version="2.0.0", kind=ModuleKind.CODE, contract_version="v1", attaches_to=(SlotName.STAFF_CAPABILITY,), provides_operations=tuple(pops), policy_actions=tuple(pops)), prov, slot=SlotName.STAFF_CAPABILITY)
    reg.mark_guarded(SlotName.STAFF_CAPABILITY)
    reg.seal()
    # Both the host (_dispatch) and the compiler (provider_for) read the active registry via
    # ``modules.registry.peek_registry``; install it as the process-active one for this test.
    monkeypatch.setattr(registry_mod, "_active", reg)
    return reg


def _host(db, snapshot, *, tenant_role="member", hooks=None, active_sop_id=None):
    profile = build_oss_local_profile()
    guard = Guard("staffdeck.runtime", profile)
    ctx = SecurityContext(principal_id="u1", tenant_id="t1", principal_type="user", tenant_role=tenant_role)
    slot = ActivationSlot(snapshot=snapshot, generation=0, turn_id="turn1", session_id="s1", active_sop_id=active_sop_id)
    host = CapabilityHost(db=db, guard=guard, security_context=ctx, slot=slot, fence=LifecycleFence(expected_generation=0))
    host.hooks = hooks
    return host


# --------------------------------------------------------------------------- 1. host-enforced PEP

def test_host_pep_denies_unbound_tool_even_when_provider_skips_guard(db, monkeypatch):
    """A tool that is in the snapshot (fence passes) but not bound to the staff must be refused by the host's
    own PEP, even though the provider never calls guard.require."""

    tool = _tool(db, "tool_a")  # exists, but NOT bound to a1 (no ensure_private_resource_binding)
    provider = _RecordingProvider()
    _install_registry(monkeypatch, provider)
    snap = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id)]))
    host = _host(db, snap)
    inv = ModuleInvocation(invocation_id="i1", module_id="tool", operation="tool.invoke/v1", arguments={"tool_id": tool.id, "order_id": "B1"}, context=_ctx(), binding_id=tool.id, side_effecting=True)
    res, receipt = host.invoke(inv)
    assert res.success is False
    assert res.error["code"] == "PERMISSION_DENIED", res.error
    assert provider.calls == [], "provider must not run when the host PEP denies"
    assert receipt is not None and receipt.status == "denied"


def test_host_pep_allows_bound_tool_and_runs_provider(db, monkeypatch):
    tool = _tool(db, "tool_b")
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    provider = _RecordingProvider()
    _install_registry(monkeypatch, provider)
    snap = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id)]))
    host = _host(db, snap)
    inv = ModuleInvocation(invocation_id="i2", module_id="tool", operation="tool.invoke/v1", arguments={"tool_id": tool.id, "order_id": "B1"}, context=_ctx(), binding_id=tool.id, side_effecting=True)
    res, receipt = host.invoke(inv)
    assert res.success is True, res.error
    assert provider.calls == ["tool.invoke/v1"]
    assert receipt is not None and receipt.status == "completed"


def test_host_pep_fails_closed_for_unmapped_operation(db, monkeypatch):
    """An operation the host cannot map to a policy action is denied, not silently allowed.

    The registry refuses to install an unknown contract in the first place (defence one), so this
    exercises the host's own guard directly (defence two): even if such an invocation reached the
    host, ``_pep`` raises PermissionDenied before any provider runs.
    """

    from staffdeck_harness.contracts.errors import PermissionDenied

    snap = CompositionCompiler(hooks=()).compile(_staff([]))
    host = _host(db, snap)
    inv = ModuleInvocation(invocation_id="i3", module_id="weather", operation="weather.lookup/v1", arguments={"city": "SH"}, context=_ctx())
    with pytest.raises(PermissionDenied) as exc:
        host._pep(inv)
    assert exc.value.details["operation"] == "weather.lookup/v1"


# --------------------------------------------------------------------------- 2. pre_tool / post_tool hooks

def test_pre_tool_deny_refuses_call_before_ledger(db, monkeypatch):
    tool = _tool(db, "tool_c")
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    provider = _RecordingProvider()
    _install_registry(monkeypatch, provider)
    snap = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id)]))
    seen: list[tuple[str, str]] = []

    def hooks(point, inv, result):
        seen.append((point, inv.operation))
        if point == "pre_tool":
            return HookDecision.deny("blocked by policy hook")
        return HookDecision.passthrough()

    host = _host(db, snap, hooks=hooks)
    inv = ModuleInvocation(invocation_id="i4", module_id="tool", operation="tool.invoke/v1", arguments={"tool_id": tool.id}, context=_ctx(), binding_id=tool.id, side_effecting=True)
    res, receipt = host.invoke(inv)
    assert res.success is False and res.error["code"] == "PRE_TOOL_DENIED"
    assert receipt is None, "a refused call must not create a ledger row"
    assert provider.calls == []
    assert seen == [("pre_tool", "tool.invoke/v1")], "post_tool must not run for a refused call"


def test_post_tool_runs_and_can_replace_result(db, monkeypatch):
    tool = _tool(db, "tool_d")
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    provider = _RecordingProvider()
    _install_registry(monkeypatch, provider)
    snap = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id)]))
    points: list[str] = []

    def hooks(point, inv, result):
        points.append(point)
        if point == "post_tool":
            assert result is not None and result.success
            return HookDecision(kind="modify", replacement=ModuleResult.ok({"redacted": True}))
        return HookDecision.passthrough()

    host = _host(db, snap, hooks=hooks)
    inv = ModuleInvocation(invocation_id="i5", module_id="tool", operation="tool.invoke/v1", arguments={"tool_id": tool.id}, context=_ctx(), binding_id=tool.id)
    res, receipt = host.invoke(inv)
    assert points == ["pre_tool", "post_tool"]
    assert res.success and res.data == {"redacted": True}
    assert receipt is not None and receipt.status == "completed"


# --------------------------------------------------------------------------- 3. finish_task closes the slot

def test_finish_task_closes_slot_and_fences_next_call(db, monkeypatch):
    tool = _tool(db, "tool_e")
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    provider = _RecordingProvider()
    _install_registry(monkeypatch, provider)
    snap = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id)]))
    host = _host(db, snap)
    fin, _ = host.invoke_proxy("finish_task", {"status": "completed", "reply_fragment": "done"}, _ctx(trace_id="hcall_fin"))
    assert fin.success and host.slot.finish["status"] == "completed"
    assert host.slot.closed is True
    later, receipt = host.invoke_proxy("tool_invoke", {"tool_id": tool.id, "arguments": {}}, _ctx(trace_id="hcall_late"))
    assert later.success is False and later.error["code"] == "ACTIVATION_FENCED"
    assert receipt is None
    assert provider.calls == [], "no capability may run after finish_task"


# --------------------------------------------------------------------------- 4. provider pinning

def test_grant_pins_provider_and_host_honours_it(db, monkeypatch):
    tool = _tool(db, "tool_f")
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    default_provider = _RecordingProvider()
    pinned_provider = _RecordingProvider()
    _install_registry(monkeypatch, default_provider, module_id="tool.local", extra=[("tool.acme", pinned_provider, ("tool.invoke/v1",))])
    snap = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id, metadata={"provider_module_id": "tool.acme"})]))
    grant = next(g for g in snap.grants if g.resource_id == tool.id)
    assert grant.provider_module_id == "tool.acme" and grant.provider_version == "2.0.0"
    host = _host(db, snap)
    inv = ModuleInvocation(invocation_id="i6", module_id="tool", operation="tool.invoke/v1", arguments={"tool_id": tool.id}, context=_ctx(), binding_id=tool.id)
    res, _ = host.invoke(inv)
    assert res.success, res.error
    assert pinned_provider.calls == ["tool.invoke/v1"] and default_provider.calls == []


def test_default_provider_is_recorded_on_grant_when_registry_present(db, monkeypatch):
    tool = _tool(db, "tool_g")
    _install_registry(monkeypatch, _RecordingProvider(), module_id="tool.local")
    snap = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id)]))
    grant = next(g for g in snap.grants if g.resource_id == tool.id)
    assert grant.provider_module_id == "tool.local" and grant.provider_version == "1.0.0"


def test_pinned_missing_provider_fails_closed(db, monkeypatch):
    tool = _tool(db, "tool_h")
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    default_provider = _RecordingProvider()
    _install_registry(monkeypatch, default_provider, module_id="tool.local")
    snap = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id, metadata={"provider_module_id": "tool.gone"})]))
    host = _host(db, snap)
    inv = ModuleInvocation(invocation_id="i7", module_id="tool", operation="tool.invoke/v1", arguments={"tool_id": tool.id}, context=_ctx(), binding_id=tool.id)
    res, _ = host.invoke(inv)
    assert res.success is False and res.error["code"] == "PROVIDER_UNAVAILABLE"
    assert default_provider.calls == [], "must not silently fall back to another provider"


def test_snapshot_id_changes_when_provider_pin_changes(db, monkeypatch):
    tool = _tool(db, "tool_i")
    _install_registry(monkeypatch, _RecordingProvider(), module_id="tool.local", extra=[("tool.acme", _RecordingProvider(), ("tool.invoke/v1",))])
    a = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id)]))
    b = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id, metadata={"provider_module_id": "tool.acme"})]))
    assert a.snapshot_id != b.snapshot_id, "the provider pin is part of the frozen contract"


# --------------------------------------------------------------------------- 5. idempotency semantics

def test_idempotency_disabled_keeps_post_side_effecting_but_not_replayable(db, monkeypatch):
    tool = _tool(db, "tool_j", method="POST", idempotency={"enabled": False})
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    captured: list[ModuleInvocation] = []

    class Capture:
        def invoke(self, host, inv):
            captured.append(inv)
            return ModuleResult.ok({"ok": True})

    _install_registry(monkeypatch, Capture())
    snap = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id)]))
    host = _host(db, snap)
    res, receipt = host.invoke_proxy("tool_invoke", {"tool_id": tool.id, "arguments": {"order_id": "B1"}}, _ctx(trace_id="hcall_idem"))
    assert res.success
    inv = captured[0]
    assert inv.side_effecting is True, "a POST is still side-effecting"
    assert inv.replayable is False
    assert inv.side_effect_key() is None, "no replay/dedupe key when idempotency is off"
    assert receipt is not None and receipt.side_effect_key is None


def test_idempotency_default_post_has_replay_key(db, monkeypatch):
    tool = _tool(db, "tool_k", method="POST")
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    captured: list[ModuleInvocation] = []

    class Capture:
        def invoke(self, host, inv):
            captured.append(inv)
            return ModuleResult.ok({"ok": True})

    _install_registry(monkeypatch, Capture())
    snap = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id)]))
    host = _host(db, snap)
    res, receipt = host.invoke_proxy("tool_invoke", {"tool_id": tool.id, "arguments": {"order_id": "B1"}}, _ctx(trace_id="hcall_idem2"))
    assert res.success
    assert captured[0].side_effecting and captured[0].replayable
    assert receipt is not None and receipt.side_effect_key, "a replayable POST carries a side-effect key"


def test_replayable_flag_default_true_on_module_invocation():
    inv = ModuleInvocation(invocation_id="x", module_id="tool", operation="tool.invoke/v1", arguments={}, context=_ctx(), side_effecting=True)
    assert inv.replayable is True and inv.side_effect_key()
    assert replace(inv, replayable=False).side_effect_key() is None


def test_capability_grant_has_provider_fields():
    g = CapabilityGrant(operation="tool.invoke/v1", resource_type="tool", resource_id="t", name="t")
    assert g.provider_module_id is None and g.provider_version is None
