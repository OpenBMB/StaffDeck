"""Module test: ``runtime.coordinator`` (对话调度器, kernel, slot runtime.kernel).

The provider is ``RuntimeCoordinatorModule`` — it hands the legacy ``AgentLoop``
to hosts that resolve it from the registry instead of importing it.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, ResourceRef
from staffdeck_harness.modules.kernel import RuntimeCoordinatorModule
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "runtime.coordinator"
CJK = re.compile(r"[一-鿿]")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1, f"{module_id} must be described exactly once"
    return hit[0]


def _placed(registry, module_id: str = MODULE_ID) -> tuple[dict, dict, dict]:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return big, sub, m
    raise AssertionError(f"{module_id} not placed in the taxonomy tree")


# --------------------------------------------------------------------------- 1. manifest


def test_manifest_runtime_coordinator(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.KERNEL
    assert item.slot is SlotName.RUNTIME_KERNEL
    assert m.attaches_to == (SlotName.RUNTIME_KERNEL,)
    assert m.provides_operations == ()
    assert m.requires_operations == ()
    assert m.policy_actions == ()
    assert m.hooks == ()
    assert isinstance(item.provider, RuntimeCoordinatorModule)
    assert item.provider.module_id == MODULE_ID
    assert item.enabled is True

    d = _described(registry)
    assert d["kind"] == "K"
    assert d["slot"] == "runtime.kernel"
    assert d["name"] == "对话调度器"
    assert d["summary"] and CJK.search(d["summary"]), "summary must be a Chinese sentence"
    assert re.match(r"^\d+\.\d+\.\d+$", d["version"])
    assert d["contract_version"] == "v1"
    assert d["provides"] == [] and d["requires"] == [] and d["policy_actions"] == [] and d["hooks"] == []
    # the fixture marks every slot guarded; 'guarded' is a slot property, not a policy_actions one
    assert d["guarded"] is True
    assert d["source"] == "builtin"


# --------------------------------------------------------------------------- 2. placement


def test_placement_runtime_coordinator(registry):
    big, sub, m = _placed(registry)
    assert big["id"] == "runtime"
    assert sub["id"] == "runtime.coordinator"
    assert sub["kind"] == "K"
    assert m["placement"] == {"big_id": "runtime", "sub_id": "runtime.coordinator", "source": "taxonomy"}
    assert m["switchable"] is False
    assert m["movable"] is False, "kernel entries stay where the taxonomy puts them"
    assert "runtime.kernel" in sub["slots"]


def test_placement_runtime_coordinator_ignores_operator_override(registry):
    # kernel modules are not movable, so an operator placement override must be ignored
    for big in tree(registry.describe(), placements={MODULE_ID: "governance.trace"}):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == MODULE_ID:
                    assert (big["id"], sub["id"]) == ("runtime", "runtime.coordinator")
                    assert m["placement"]["source"] == "taxonomy"
                    return
    raise AssertionError("module vanished from the tree")


# --------------------------------------------------------------------------- 3. disable


def test_disable_runtime_coordinator_not_switchable(registry, settings):
    d = _described(registry)
    assert d["kind"] == "K"
    assert d["switchable"] is False, "admin API refuses to disable non-switchable modules (是平台核心组成部分，不能停用)"
    # the admin endpoint keys its refusal on describe()['switchable'] — mirror that predicate here
    assert not (d["kind"] == "A" or d["metadata"].get("switchable"))


# --------------------------------------------------------------------------- 4. provider


def test_provider_runtime_coordinator_loop_returns_agent_loop(module, db):
    from app.core.agent_loop import AgentLoop

    provider = module(MODULE_ID).provider
    loop = provider.loop(db)
    assert isinstance(loop, AgentLoop)
    assert loop.db is db
    assert loop.events is not None
    assert loop.stream_sink is None
    assert loop.stream_delivery_succeeded is False


def test_provider_runtime_coordinator_loop_forwards_kwargs(module, db):
    from app.core.agent_loop import AgentLoop

    seen: list[tuple[str, dict]] = []
    sink = SimpleNamespace(name="stream")
    loop = module(MODULE_ID).provider.loop(db, event_sink=lambda et, payload: seen.append((et, payload)), stream_sink=sink)
    assert isinstance(loop, AgentLoop)
    assert loop.stream_sink is sink
    # the event sink handed through kwargs is wired to the loop's EventLog and fires on record()
    loop.events.record("t1", "s1", "probe_event", {"k": "v"})
    assert seen == [("probe_event", {"k": "v"})]
    db.rollback()


def test_provider_runtime_coordinator_loop_opens_legacy_engine(module, db, monkeypatch):
    """The loop built here is what the engine host wraps; with Harness v3 disabled it opens Harness v2."""

    from staffdeck_harness.bridge.engine_host import EngineHost
    from app.session.session_schema import ChatTurnRequest
    import app.core.agent_loop as agent_loop_mod

    monkeypatch.setattr(agent_loop_mod, "get_settings", lambda: SimpleNamespace(harness_v3_enabled=False))
    sentinel = object()
    seen = []
    monkeypatch.setattr(EngineHost, "open", lambda self, loop, request, agent_id: seen.append(loop) or sentinel)
    loop = module(MODULE_ID).provider.loop(db)
    request = ChatTurnRequest(tenant_id="t1", user_id="u1", agent_id="a1", message="hi", channel="web")
    engine = loop._open_engine(request)
    assert engine is sentinel and seen == [loop]


# --------------------------------------------------------------------------- 5. events / pep


def test_events_or_pep_runtime_coordinator_no_policy_actions(registry, guard, security_ctx):
    d = _described(registry)
    assert d["policy_actions"] == [], "kernel coordinator declares no policy actions of its own"
    # the turn contract the coordinator drives (runtime.turn/v1) is still mapped, and the OSS_LOCAL
    # profile denies it across tenants — the coordinator cannot smuggle a cross-tenant turn
    assert "runtime.turn/v1" in DEFAULT_ACTION_MAP
    g = guard(MODULE_ID)
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(tenant_id="t1"), "runtime.turn/v1", ResourceRef(type="runtime", id="s1", tenant_id="t2"))
    assert "tenant boundary" in str(exc.value)
    same = g.require(security_ctx(tenant_id="t1"), "runtime.turn/v1", ResourceRef(type="runtime", id="s1", tenant_id="t1"))
    assert same.allowed


def test_events_or_pep_runtime_coordinator_unknown_operation_rejected(guard, security_ctx):
    with pytest.raises(PermissionDenied):
        guard(MODULE_ID).require(security_ctx(), "runtime.coordinate/v1", ResourceRef(type="runtime", id="s1", tenant_id="t1"))


# --------------------------------------------------------------------------- extra: registry invariants


def test_runtime_coordinator_stays_enabled_regardless_of_engine_choice(settings):
    settings.harness_v3_enabled = True
    reg = discover_and_install(ModuleRegistry(), settings)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is True
    assert [i.manifest.module_id for i in reg.providers(SlotName.RUNTIME_KERNEL)] == ["runtime.coordinator", "harness_v3.core"]
