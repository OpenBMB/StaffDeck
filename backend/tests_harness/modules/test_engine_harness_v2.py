"""Module test: ``engine.harness_v2`` (Harness v2 引擎, trusted, slot runtime.engine).

Provider ``LegacyEngine.open(loop, request, agent_id)`` returns the in-process
``HarnessV2Engine``. It is the deployment default whenever ``harness_v3_enabled`` is
False, and exactly one ``runtime.engine`` module may be active.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, ResourceRef
from staffdeck_harness.modules.builtin import LegacyEngine
from staffdeck_harness.modules.registry import ModuleRegistry, SlotConflict, discover_and_install
from staffdeck_harness.modules.taxonomy import FIXED_SLOTS, tree

MODULE_ID = "engine.harness_v2"
CJK = re.compile(r"[一-鿿]")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def _placed(registry, module_id: str = MODULE_ID) -> tuple[dict, dict, dict]:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return big, sub, m
    raise AssertionError(f"{module_id} not placed")


def _guarded_registry(settings) -> ModuleRegistry:
    reg = discover_and_install(ModuleRegistry(), settings)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    return reg


@pytest.fixture
def fake_loop(db):
    """The ``owner`` shape HarnessV2Engine reads in its constructor (see app/core/harness_v2_engine.py)."""

    recorded: list[tuple] = []
    events = SimpleNamespace(record=lambda *a, **k: recorded.append(a), recorded=recorded)
    return SimpleNamespace(db=db, events=events)


# --------------------------------------------------------------------------- 1. manifest


def test_manifest_engine_harness_v2(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.TRUSTED
    assert item.slot is SlotName.RUNTIME_ENGINE
    assert m.attaches_to == (SlotName.RUNTIME_ENGINE,)
    assert m.provides_operations == ("runtime.turn/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ()
    assert m.hooks == ()
    assert isinstance(item.provider, LegacyEngine)
    assert item.provider.module_id == MODULE_ID
    assert item.enabled is True, "harness_v3_enabled=False → Harness v2 is the active engine"

    d = _described(registry)
    assert d["kind"] == "T"
    assert d["slot"] == "runtime.engine"
    assert d["name"] == "Harness v2 引擎"
    assert d["summary"] and CJK.search(d["summary"])
    assert re.match(r"^\d+\.\d+\.\d+$", d["version"])
    assert d["provides"] == ["runtime.turn/v1"]
    assert d["requires"] == [] and d["policy_actions"] == [] and d["hooks"] == []
    # 'guarded' reflects the slot: runtime.engine is marked guarded by builtin.register itself
    assert d["guarded"] is True
    assert d["enabled"] is True


def test_manifest_engine_harness_v2_slot_is_guarded_even_without_fixture_marks(settings):
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    assert _described(reg)["guarded"] is True


# --------------------------------------------------------------------------- 2. placement


def test_placement_engine_harness_v2(registry):
    big, sub, m = _placed(registry)
    assert big["id"] == "runtime"
    assert sub["id"] == "runtime.bridge"
    assert sub["kind"] == "T"
    assert sub["slots"] == ["runtime.engine"]
    assert m["placement"] == {"big_id": "runtime", "sub_id": "runtime.bridge", "source": "slot"}
    assert m["switchable"] is False
    assert m["movable"] is False, "engines are pinned to the fixed runtime.engine slot"
    assert "runtime.engine" in FIXED_SLOTS


def test_placement_engine_harness_v2_shares_sub_with_harness_v3_engine(registry):
    _, sub, _ = _placed(registry)
    ids = sorted(x["module_id"] for x in sub["modules"])
    assert ids == ["engine.harness_v2", "engine.harness_v3"]
    assert sub["enabled"] == 1 and sub["total"] == 2, "exactly one engine active at a time"


# --------------------------------------------------------------------------- 3. disable


def test_disable_engine_harness_v2_not_switchable(registry):
    d = _described(registry)
    assert d["kind"] == "T"
    assert d["switchable"] is False
    assert d["metadata"].get("switchable") is None
    # admin.py refuses runtime.engine entries with "通过选择引擎 / 权限模式切换，不能单独停用"
    assert d["slot"] in {"runtime.engine", "security.pep"}


def test_disable_engine_harness_v2_follows_harness_v3_enabled(settings):
    settings.harness_v3_enabled = True
    reg = _guarded_registry(settings)
    legacy = reg.get(MODULE_ID)
    assert legacy is not None and legacy.enabled is False
    active = reg.provider(SlotName.RUNTIME_ENGINE)
    assert active is not None and active.manifest.module_id == "engine.harness_v3"
    assert MODULE_ID not in [i.manifest.module_id for i in reg.providers(SlotName.RUNTIME_ENGINE)]


def test_disable_engine_harness_v2_single_provider_slot_rejects_two_engines(settings):
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.set_enabled("engine.harness_v3", True)  # both engines on → seal must refuse
    with pytest.raises(SlotConflict):
        reg.seal()


# --------------------------------------------------------------------------- 4. provider


def test_provider_engine_harness_v2_open_returns_harness_v2_engine(module, fake_loop, db):
    from app.core.harness_v2_engine import HarnessV2Engine

    engine = module(MODULE_ID).provider.open(fake_loop, None, "a1")
    assert type(engine) is HarnessV2Engine
    assert engine.owner is fake_loop
    assert engine.db is db
    assert engine.events is fake_loop.events
    # fresh per-turn state
    assert engine.session is None and engine.turn_record is None and engine.user_message_id is None
    assert engine.active_frame_id is None and engine.active_run_id is None
    assert engine.task_agent is not None and engine.store is not None


def test_provider_engine_harness_v2_open_is_fresh_per_call(module, fake_loop):
    provider = module(MODULE_ID).provider
    a = provider.open(fake_loop, None, "a1")
    b = provider.open(fake_loop, None, None)
    assert a is not b
    assert type(a) is type(b)


def test_provider_engine_harness_v2_open_ignores_request_and_agent(module, fake_loop):
    """The Harness v2 engine reads everything from the loop; request/agent_id are accepted for the contract only."""

    from app.core.harness_v2_engine import HarnessV2Engine
    from app.session.session_schema import ChatTurnRequest

    request = ChatTurnRequest(tenant_id="t1", user_id="u1", agent_id="a1", message="hi")
    engine = module(MODULE_ID).provider.open(fake_loop, request, "someone-else")
    assert isinstance(engine, HarnessV2Engine)
    assert not hasattr(engine, "snapshot"), "Harness v2 engine carries no DSH composition snapshot"


def test_provider_engine_harness_v2_is_what_engine_host_opens_by_default(module, fake_loop, settings):
    from app.core.harness_v2_engine import HarnessV2Engine
    from app.session.session_schema import ChatTurnRequest
    from staffdeck_harness.bridge.engine_host import EngineHost

    host = EngineHost(settings)
    assert host.harness_v3_enabled is False
    request = ChatTurnRequest(tenant_id="t1", user_id="u1", agent_id="a1", message="hi")
    engine = host.open(fake_loop, request, "a1")
    assert type(engine) is HarnessV2Engine
    assert type(engine) is type(module(MODULE_ID).provider.open(fake_loop, request, "a1"))


# --------------------------------------------------------------------------- 5. events / pep


def test_events_or_pep_engine_harness_v2_turn_contract_maps_and_denies_cross_tenant(registry, guard, security_ctx):
    d = _described(registry)
    assert d["policy_actions"] == []
    # engine.harness_v2 provides runtime.turn/v1; the contract is mapped through DEFAULT_ACTION_MAP …
    assert DEFAULT_ACTION_MAP["runtime.turn/v1"] == ("execute", "runtime")
    # … and OSS_LOCAL enforces the tenant boundary on it
    g = guard(MODULE_ID)
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(tenant_id="t1"), "runtime.turn/v1", ResourceRef(type="runtime", id="turn1", tenant_id="t2"))
    assert exc.value.details["profile"] == "OSS_LOCAL"
    assert g.require(security_ctx(tenant_id="t1"), "runtime.turn/v1", ResourceRef(type="runtime", id="turn1", tenant_id="t1")).allowed
