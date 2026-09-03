"""Per-module tests for ``observer.trace`` (执行轨迹记录, kind T, slot event.observer)."""

from __future__ import annotations

import re

import pytest

from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper
from staffdeck_harness.events import relay as relay_mod
from staffdeck_harness.events.relay import SessionEventRelay, fanout_event
from staffdeck_harness.modules import registry as registry_mod
from staffdeck_harness.modules.builtin import TraceObserver
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "observer.trace"
CJK = re.compile(r"[一-鿿]")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def _placement(registry, module_id: str = MODULE_ID) -> tuple[dict, dict, dict]:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return big, sub, m
    raise AssertionError(f"{module_id} not placed in the taxonomy tree")


# --------------------------------------------------------------------------- 1. manifest


def test_manifest_observer_trace(registry, module) -> None:
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.TRUSTED
    assert item.slot is SlotName.EVENT_OBSERVER
    assert m.attaches_to == (SlotName.EVENT_OBSERVER,)
    assert m.provides_operations == ("event.observe/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ()
    assert m.hooks == ()
    assert isinstance(item.provider, TraceObserver)

    d = _described(registry)
    assert d["kind"] == "T" and d["slot"] == "event.observer"
    assert d["name"] == "执行轨迹记录"
    assert d["summary"] and CJK.search(d["summary"])
    assert registry_mod.SEMVER_RE.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["provides"] == ["event.observe/v1"] and d["requires"] == [] and d["hooks"] == [] and d["policy_actions"] == []
    assert d["enabled"] is True and d["source"] == "builtin"
    # the fixture marks every slot guarded, but the module itself declares no policy actions
    assert d["guarded"] is True and not d["policy_actions"]


def test_manifest_observer_trace_unguarded_without_pep_host(settings) -> None:
    """Without any guarded slot the module still seals: no policy actions means no PEP binding is required."""

    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    d = _described(reg)
    assert d["guarded"] is False and d["policy_actions"] == []


# --------------------------------------------------------------------------- 2. placement


def test_placement_observer_trace(registry) -> None:
    big, sub, m = _placement(registry)
    assert big["id"] == "governance" and big["name"] == "运行治理"
    assert sub["id"] == "governance.trace" and sub["kind"] == "T"
    assert m["placement"] == {"big_id": "governance", "sub_id": "governance.trace", "source": "taxonomy"}
    assert m["switchable"] is False
    assert m["movable"] is True  # T modules outside the fixed slots can be re-parented for display
    assert sub["total"] == 1 and sub["enabled"] == 1


def test_placement_observer_trace_override_only_affects_display(registry) -> None:
    forest = tree(registry.describe(), {MODULE_ID: "governance.monitoring"})
    placed = [(b["id"], s["id"], x["placement"]) for b in forest for s in b["subs"] for x in s["modules"] if x["module_id"] == MODULE_ID]
    assert placed == [("governance", "governance.monitoring", {"big_id": "governance", "sub_id": "governance.monitoring", "source": "override"})]


# --------------------------------------------------------------------------- 3. disable


def test_disable_observer_trace_is_not_switchable(registry, settings) -> None:
    d = _described(registry)
    assert d["kind"] == "T" and d["switchable"] is False
    # The admin API refuses to add non-switchable modules to disabled_modules ("是平台核心组成部分，不能停用").
    # The registry itself still honours the setting, so the admin validation is the only gate.
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    assert reg.get(MODULE_ID) is not None and reg.get(MODULE_ID).enabled is False


# --------------------------------------------------------------------------- 4. provider


def test_provider_observer_trace(module) -> None:
    provider = module(MODULE_ID).provider
    assert isinstance(provider, TraceObserver)
    assert provider.name == MODULE_ID
    assert isinstance(provider, relay_mod.EventObserver)
    # Trace persistence is done by the relay's trace sink; the observer is a no-op consumer.
    assert provider.on_event("t1", "s1", "harness_action_created", {"action": "tool", "tool_name": "x"}) is None
    assert provider.on_event("t1", "s1", "harness_v3_turn_ended", {}) is None


def test_provider_observer_trace_receives_relayed_events(registry, monkeypatch) -> None:
    """The relay fans out every translated DSH event to the registry-installed observer."""

    monkeypatch.setattr(registry_mod, "_active", registry)
    monkeypatch.setattr(relay_mod, "_observers", {})
    provider = registry.get(MODULE_ID).provider
    assert provider in relay_mod._registry_observers()

    seen: list[tuple] = []
    monkeypatch.setattr(provider, "on_event", lambda *a: seen.append(a), raising=False)
    sink: list[tuple] = []
    relay = SessionEventRelay("t1", "s1", lambda et, p: sink.append((et, p)))
    relay({"type": "turn/start", "data": {"turn": 1}})
    relay({"type": "assistant/chunk", "data": {"text": "你好"}})
    relay({"type": "tool/call", "data": {"name": "search", "arguments": '{"q": 1}', "callId": "c1"}})
    relay({"type": "unknown/kind", "data": {}})
    assert relay.count == 3
    assert [et for et, _ in sink] == ["harness_v3_turn_started", "stream_delta", "harness_action_created"]
    assert [ev[2] for ev in seen] == ["harness_v3_turn_started", "stream_delta", "harness_action_created"]
    assert seen[0][:2] == ("t1", "s1")
    assert seen[2][3]["arguments"] == {"q": 1}


# --------------------------------------------------------------------------- 5. events / pep


def test_events_observer_trace_failure_never_breaks_the_turn(registry, monkeypatch) -> None:
    monkeypatch.setattr(registry_mod, "_active", registry)
    monkeypatch.setattr(relay_mod, "_observers", {})
    provider = registry.get(MODULE_ID).provider

    def boom(*a):
        raise RuntimeError("observer down")

    monkeypatch.setattr(provider, "on_event", boom, raising=False)
    fanout_event("t1", "s1", "harness_tool_result", {"call_id": "c1"})  # must not raise


def test_events_observer_trace_disabled_module_is_not_fanned_out(settings, monkeypatch) -> None:
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    monkeypatch.setattr(relay_mod, "_observers", {})
    provider = reg.get(MODULE_ID).provider
    assert provider not in relay_mod._registry_observers()
    assert MODULE_ID not in [i.manifest.module_id for i in reg.providers(SlotName.EVENT_OBSERVER)]


def test_pep_observer_trace_declares_no_policy_actions(module) -> None:
    """Observers only consume; ``event.observe/v1`` is deliberately absent from the PEP action map."""

    m = module(MODULE_ID).manifest
    assert m.policy_actions == ()
    with pytest.raises(KeyError):
        PolicyActionMapper(DEFAULT_ACTION_MAP).map("event.observe/v1")
