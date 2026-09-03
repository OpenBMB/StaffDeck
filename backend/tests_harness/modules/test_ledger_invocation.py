"""Per-module tests for ``ledger.invocation`` (调用记录, kind T, slot event.observer)."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from app.db.models import HarnessInvocationRecord
from staffdeck_harness.capabilities.ledger import InvocationLedger, LedgerEntry
from staffdeck_harness.contracts.errors import OutcomeUnknown
from staffdeck_harness.contracts.invocation import ModuleResult, Receipt
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper
from staffdeck_harness.events import relay as relay_mod
from staffdeck_harness.events.relay import fanout_event
from staffdeck_harness.modules import registry as registry_mod
from staffdeck_harness.modules.kernel import InvocationLedgerModule
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "ledger.invocation"
CJK = re.compile(r"[一-鿿]")


def _inv(invocation, iid: str, *a, **kw):
    """The shared fixture always stamps ``inv1``; ledger rows are unique on (run_id, call_id)."""

    return replace(invocation(*a, **kw), invocation_id=iid)


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


def test_manifest_ledger_invocation(registry, module) -> None:
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
    assert isinstance(item.provider, InvocationLedgerModule)
    assert item.provider.module_id == MODULE_ID

    d = _described(registry)
    assert d["kind"] == "T" and d["slot"] == "event.observer"
    assert d["name"] == "调用记录"
    assert d["summary"] and CJK.search(d["summary"])
    assert registry_mod.SEMVER_RE.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["provides"] == ["event.observe/v1"] and d["requires"] == [] and d["hooks"] == [] and d["policy_actions"] == []
    assert d["enabled"] is True and d["source"] == "builtin"
    assert d["guarded"] is True and not d["policy_actions"]


def test_manifest_ledger_invocation_unguarded_without_pep_host(settings) -> None:
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    d = _described(reg)
    assert d["guarded"] is False and d["policy_actions"] == []


# --------------------------------------------------------------------------- 2. placement


def test_placement_ledger_invocation(registry) -> None:
    big, sub, m = _placement(registry.describe())
    assert big["id"] == "governance" and big["name"] == "运行治理"
    assert sub["id"] == "governance.monitoring" and sub["kind"] == "T"
    assert "harness_invocations" in sub["legacy"]
    assert m["placement"] == {"big_id": "governance", "sub_id": "governance.monitoring", "source": "taxonomy"}
    assert m["switchable"] is False
    assert m["movable"] is True
    assert sub["total"] == 1 and sub["enabled"] == 1


def test_placement_ledger_invocation_override_only_affects_display(registry) -> None:
    big, sub, m = _placement(registry.describe(), placements={MODULE_ID: "governance.trace"})
    assert (big["id"], sub["id"], m["placement"]["source"]) == ("governance", "governance.trace", "override")
    # resolution is untouched by display overrides
    assert registry.for_operation("event.observe/v1") is not None


# --------------------------------------------------------------------------- 3. disable


def test_disable_ledger_invocation_is_not_switchable(registry, settings) -> None:
    d = _described(registry)
    assert d["kind"] == "T" and d["switchable"] is False
    # The admin API refuses non-switchable ids in disabled_modules ("是平台核心组成部分，不能停用");
    # the registry itself would honour the setting, so that validation is the only gate.
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    assert reg.get(MODULE_ID) is not None and reg.get(MODULE_ID).enabled is False


# --------------------------------------------------------------------------- 4. provider


def test_provider_ledger_invocation_builds_ledger(module, db) -> None:
    provider = module(MODULE_ID).provider
    ledger = provider.ledger(db)
    assert isinstance(ledger, InvocationLedger)
    assert ledger.db is db
    # each call is a fresh ledger over the given session (no hidden state)
    assert provider.ledger(db) is not ledger


def test_provider_ledger_invocation_start_finish_round_trip(module, db, invocation) -> None:
    ledger = module(MODULE_ID).provider.ledger(db)
    inv = invocation("tool.invoke/v1", module_id="tool", arguments={"tool_id": "t1", "amount": 5}, binding_id="t1", side_effecting=True)
    entry = ledger.start(inv)
    assert isinstance(entry, LedgerEntry)
    row = entry.row
    assert isinstance(row, HarnessInvocationRecord)
    assert row.status == "started"
    assert row.tenant_id == "t1" and row.session_id == "s1" and row.task_id == "tf1" and row.run_id == "run1"
    assert row.call_id == "inv1" and row.tool_name == "tool:tool.invoke/v1"
    assert row.request_digest == inv.request_digest()
    assert row.logical_action_key == inv.side_effect_key()
    assert row.arguments_json == {"tool_id": "t1", "amount": 5}
    assert row.approval_json == {"engine": "harness_v3", "snapshot_id": None, "binding_id": "t1"}

    receipt = ledger.finish(entry, ModuleResult.ok({"order": "o1"}, citations=({"source": "x"},)))
    assert isinstance(receipt, Receipt)
    assert receipt.status == "completed" and receipt.invocation_id == "inv1"
    assert receipt.ledger_id == row.id and receipt.request_digest == inv.request_digest()
    assert receipt.side_effect_key == inv.side_effect_key() and receipt.replayed_from is None
    assert receipt.finished_at is not None and receipt.error is None
    persisted = db.get(HarnessInvocationRecord, row.id)
    assert persisted.status == "completed"
    assert persisted.response_cache_json["success"] is True and persisted.response_cache_json["data"] == {"order": "o1"}
    assert persisted.result_json["citations"] == [{"source": "x"}]


def test_provider_ledger_invocation_replay_and_failure_states(module, db, invocation) -> None:
    ledger = module(MODULE_ID).provider.ledger(db)
    args = {"tool_id": "t1", "amount": 5}
    first = ledger.start(_inv(invocation, "c1", "tool.invoke/v1", module_id="tool", arguments=args, binding_id="t1", side_effecting=True))
    ledger.finish(first, ModuleResult.ok({"order": "o1"}))
    # same logical action replays the cached result instead of re-executing
    replay = ledger.replay_or_block(_inv(invocation, "c2", "tool.invoke/v1", module_id="tool", arguments=args, binding_id="t1", side_effecting=True))
    assert replay is not None
    result, receipt = replay
    assert result.success and result.data == {"order": "o1", "idempotent_replay": True, "replayed_from_invocation_id": first.row.id}
    assert receipt.replayed_from == first.row.id and receipt.status == "completed"

    # a "not sent" failure releases the claim; a maybe-sent failure blocks with outcome_unknown
    inv2 = _inv(invocation, "f1", "tool.invoke/v1", module_id="tool", arguments={"tool_id": "t1", "amount": 6}, binding_id="t1", side_effecting=True, step_id="n2")
    e2 = ledger.start(inv2)
    r2 = ledger.finish(e2, ModuleResult.fail("PERMISSION_DENIED", "no"))
    assert r2.status == "failed" and r2.side_effect_key is None and r2.error == {"code": "PERMISSION_DENIED", "message": "no"}
    e3 = ledger.start(replace(inv2, invocation_id="u1"))
    r3 = ledger.finish(e3, ModuleResult.fail("TIMEOUT", "gateway"))
    assert r3.status == "outcome_unknown" and r3.side_effect_key == inv2.side_effect_key()
    with pytest.raises(OutcomeUnknown):
        ledger.replay_or_block(inv2)
    unknown = ledger.unknown_outcomes("t1")
    assert [x.id for x in unknown] == [e3.row.id]
    assert ledger.unknown_outcomes("other-tenant") == []
    ledger.reconcile(unknown[0], status="completed", result={"success": True, "data": {"order": "o2"}})
    assert ledger.replay_or_block(inv2)[0].data["order"] == "o2"


def test_provider_ledger_invocation_deny_and_cancel(module, db, invocation) -> None:
    ledger = module(MODULE_ID).provider.ledger(db)
    inv = _inv(invocation, "d1", "sandbox.execute/v1", module_id="sandbox", arguments={"cmd": "ls"}, side_effecting=True)
    denied = ledger.deny(ledger.start(inv), {"code": "PERMISSION_DENIED", "message": "cross tenant"})
    assert denied.status == "denied" and denied.side_effect_key is None and denied.error["code"] == "PERMISSION_DENIED"
    assert ledger.replay_or_block(inv) is None
    cancelled = ledger.cancel(ledger.start(replace(inv, invocation_id="d2")))
    assert cancelled.status == "cancelled" and cancelled.side_effect_key is None
    assert ledger.replay_or_block(inv) is None
    # read-only invocations get a receipt but no side-effect key
    ro = _inv(invocation, "r1", "knowledge.search/v1", module_id="knowledge", arguments={"q": "hi"})
    receipt = ledger.finish(ledger.start(ro), ModuleResult.fail("TIMEOUT", "x"))
    assert receipt.status == "failed" and receipt.side_effect_key is None


# --------------------------------------------------------------------------- 5. events / pep


def test_events_ledger_invocation_relay_skips_provider_without_on_event(registry, monkeypatch) -> None:
    """The relay only fans out to providers exposing ``on_event``; the ledger module never breaks a turn."""

    monkeypatch.setattr(registry_mod, "_active", registry)
    monkeypatch.setattr(relay_mod, "_observers", {})
    provider = registry.get(MODULE_ID).provider
    assert provider in [i.provider for i in registry.providers(SlotName.EVENT_OBSERVER)]
    assert provider in relay_mod._registry_observers()   # listens (no-op) so the manifest's event.observe/v1 is honoured
    fanout_event("t1", "s1", "harness_tool_result", {"call_id": "c1"})  # must not raise


def test_events_ledger_invocation_provider_is_an_event_observer(module) -> None:
    provider = module(MODULE_ID).provider
    assert isinstance(provider, relay_mod.EventObserver)


def test_pep_ledger_invocation_declares_no_policy_actions(module) -> None:
    """The ledger records decisions made by the capability host's Guard; it holds no PEP actions itself."""

    m = module(MODULE_ID).manifest
    assert m.policy_actions == ()
    with pytest.raises(KeyError):
        PolicyActionMapper(DEFAULT_ACTION_MAP).map("event.observe/v1")
