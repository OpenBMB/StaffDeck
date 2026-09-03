"""Per-module tests for ``observer.feedback`` (反馈收集, kind A, slot event.observer)."""

from __future__ import annotations

import re

import pytest

from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper
from staffdeck_harness.events import relay as relay_mod
from staffdeck_harness.events.relay import SessionEventRelay, fanout_event
from staffdeck_harness.modules import registry as registry_mod
from staffdeck_harness.modules.builtin import FeedbackObserver
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "observer.feedback"
CJK = re.compile(r"[一-鿿]")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def _placement(registry_modules: list[dict], module_id: str = MODULE_ID, placements: dict | None = None) -> tuple[dict, dict, dict]:
    for big in tree(registry_modules, placements):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return big, sub, m
    raise AssertionError(f"{module_id} not placed in the taxonomy tree")


# --------------------------------------------------------------------------- 1. manifest


def test_manifest_observer_feedback(registry, module) -> None:
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE
    assert item.slot is SlotName.EVENT_OBSERVER
    assert m.attaches_to == (SlotName.EVENT_OBSERVER,)
    assert m.provides_operations == ("event.observe/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ()
    assert m.hooks == ()
    assert isinstance(item.provider, FeedbackObserver)

    d = _described(registry)
    assert d["kind"] == "A" and d["slot"] == "event.observer"
    assert d["name"] == "反馈收集"
    assert d["summary"] and CJK.search(d["summary"])
    assert registry_mod.SEMVER_RE.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["provides"] == ["event.observe/v1"] and d["requires"] == [] and d["hooks"] == [] and d["policy_actions"] == []
    assert d["enabled"] is True and d["source"] == "builtin"
    assert d["guarded"] is True and not d["policy_actions"]


def test_manifest_observer_feedback_unguarded_without_pep_host(settings) -> None:
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    d = _described(reg)
    assert d["guarded"] is False and d["policy_actions"] == []


# --------------------------------------------------------------------------- 2. placement


def test_placement_observer_feedback(registry) -> None:
    big, sub, m = _placement(registry.describe())
    assert big["id"] == "governance" and big["name"] == "运行治理"
    assert sub["id"] == "governance.feedback" and sub["kind"] == "A"
    assert m["placement"] == {"big_id": "governance", "sub_id": "governance.feedback", "source": "taxonomy"}
    assert m["switchable"] is True
    assert m["movable"] is True
    assert sub["total"] == 1 and sub["enabled"] == 1


def test_placement_observer_feedback_override_and_disabled_counts(registry, settings) -> None:
    big, sub, m = _placement(registry.describe(), placements={MODULE_ID: "governance.monitoring"})
    assert (big["id"], sub["id"], m["placement"]["source"]) == ("governance", "governance.monitoring", "override")
    # unknown override falls back to the curated taxonomy
    big, sub, m = _placement(registry.describe(), placements={MODULE_ID: "nope.sub"})
    assert (sub["id"], m["placement"]["source"]) == ("governance.feedback", "taxonomy")
    # a disabled module still shows in its sub-module, counted in total but not enabled
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    big, sub, m = _placement(reg.describe())
    assert m["enabled"] is False and sub["total"] == 1 and sub["enabled"] == 0


# --------------------------------------------------------------------------- 3. disable


def test_disable_observer_feedback(registry, settings) -> None:
    assert _described(registry)["switchable"] is True
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert isinstance(item.provider, FeedbackObserver)
    assert _described(reg)["enabled"] is False
    assert MODULE_ID not in [i.manifest.module_id for i in reg.providers(SlotName.EVENT_OBSERVER)]
    # the other observers keep working
    assert {i.manifest.module_id for i in reg.providers(SlotName.EVENT_OBSERVER)} >= {"observer.trace", "ledger.invocation"}


def test_disable_observer_feedback_via_env_and_comma_list(settings, monkeypatch) -> None:
    settings.harness_disabled_modules = " observer.feedback , unknown.module "
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    assert reg.get(MODULE_ID).enabled is False
    assert reg.get("observer.trace").enabled is True


def test_disable_observer_feedback_refused_after_seal(registry) -> None:
    with pytest.raises(registry_mod.RegistrySealed):
        registry.set_enabled(MODULE_ID, False)


# --------------------------------------------------------------------------- 4. provider


def test_provider_observer_feedback(module) -> None:
    provider = module(MODULE_ID).provider
    assert isinstance(provider, FeedbackObserver)
    assert provider.name == MODULE_ID
    assert isinstance(provider, relay_mod.EventObserver)
    # Feedback analysis is enqueued by the legacy finalize path; the built-in observer is a no-op consumer.
    assert provider.on_event("t1", "s1", "harness_v3_assistant_message", {"interrupted": False, "execution_engine": "harness_v3"}) is None
    assert provider.on_event("t1", "s1", "harness_v3_turn_ended", {"turn": 1, "reason": "stop"}) is None


def test_provider_observer_feedback_receives_relayed_events(registry, monkeypatch) -> None:
    monkeypatch.setattr(registry_mod, "_active", registry)
    monkeypatch.setattr(relay_mod, "_observers", {})
    provider = registry.get(MODULE_ID).provider
    assert provider in relay_mod._registry_observers()

    seen: list[tuple] = []
    monkeypatch.setattr(provider, "on_event", lambda *a: seen.append(a), raising=False)
    relay = SessionEventRelay("t1", "s1", None)
    relay({"type": "assistant/message", "data": {"interrupted": True}})
    relay({"type": "turn/end", "data": {"turn": 2, "finishReason": "stop"}})
    relay({"type": "tool/result", "data": {"message": {"content": [{"type": "tool-result", "toolCallId": "c1", "isError": True}]}}})
    assert relay.count == 3
    assert [ev[2] for ev in seen] == ["harness_v3_assistant_message", "harness_v3_turn_ended", "harness_tool_result"]
    assert seen[0][3] == {"interrupted": True, "execution_engine": "harness_v3"}
    assert seen[1][3] == {"turn": 2, "reason": "stop"}
    assert seen[2][3] == {"call_id": "c1", "is_error": True, "execution_engine": "harness_v3"}


def test_provider_observer_feedback_can_be_replaced_by_explicit_registration(registry, monkeypatch) -> None:
    """A deployment can swap the consumer: an explicitly registered observer with the same name wins, no duplicates."""

    monkeypatch.setattr(registry_mod, "_active", registry)
    monkeypatch.setattr(relay_mod, "_observers", {})

    class Custom:
        name = MODULE_ID
        calls: list[str] = []

        def on_event(self, tenant_id, session_id, event_type, payload):
            self.calls.append(event_type)

    custom = Custom()
    relay_mod.register_observer(custom)
    builtin_seen: list[str] = []
    monkeypatch.setattr(registry.get(MODULE_ID).provider, "on_event", lambda *a: builtin_seen.append(a[2]), raising=False)
    fanout_event("t1", "s1", "harness_v3_turn_ended", {"turn": 1})
    assert custom.calls == ["harness_v3_turn_ended"]
    assert builtin_seen == ["harness_v3_turn_ended"]  # registry observers are distinct objects, both consume


# --------------------------------------------------------------------------- 5. events / pep


def test_events_observer_feedback_failure_never_breaks_the_turn(registry, monkeypatch) -> None:
    monkeypatch.setattr(registry_mod, "_active", registry)
    monkeypatch.setattr(relay_mod, "_observers", {})
    provider = registry.get(MODULE_ID).provider

    def boom(*a):
        raise RuntimeError("feedback queue down")

    monkeypatch.setattr(provider, "on_event", boom, raising=False)
    other_seen: list[str] = []
    monkeypatch.setattr(registry.get("observer.trace").provider, "on_event", lambda *a: other_seen.append(a[2]), raising=False)
    fanout_event("t1", "s1", "harness_v3_turn_ended", {"turn": 1})  # must not raise
    assert other_seen == ["harness_v3_turn_ended"]  # siblings still receive the event


def test_events_observer_feedback_disabled_module_is_not_fanned_out(settings, monkeypatch) -> None:
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    monkeypatch.setattr(relay_mod, "_observers", {})
    provider = reg.get(MODULE_ID).provider
    seen: list[str] = []
    monkeypatch.setattr(provider, "on_event", lambda *a: seen.append(a[2]), raising=False)
    fanout_event("t1", "s1", "harness_v3_turn_ended", {"turn": 1})
    assert seen == []


def test_pep_observer_feedback_declares_no_policy_actions(module) -> None:
    m = module(MODULE_ID).manifest
    assert m.policy_actions == ()
    with pytest.raises(KeyError):
        PolicyActionMapper(DEFAULT_ACTION_MAP).map("event.observe/v1")
