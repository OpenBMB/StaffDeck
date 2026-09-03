"""Module tests: ``runtime.cancellation`` (中断与恢复) — CancellationModule over the legacy cancel markers."""

from __future__ import annotations

import re

import pytest

from app.core import cancellation as legacy
from app.db.models import AgentEvent, HarnessTurnRecord, utc_now
from staffdeck_dsh.contracts.manifest import ModuleKind, SlotName
from staffdeck_dsh.contracts.security import DEFAULT_ACTION_MAP
from staffdeck_dsh.modules.kernel import CancellationModule
from staffdeck_dsh.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_dsh.modules.taxonomy import FIXED_SLOTS, tree

from .conftest import FakeSettings

MODULE_ID = "runtime.cancellation"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")


def _describe(registry, module_id: str = MODULE_ID) -> dict:
    return next(m for m in registry.describe() if m["module_id"] == module_id)


def _placed(registry, module_id: str = MODULE_ID) -> dict:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return m
    raise AssertionError(f"{module_id} not placed in the taxonomy tree")


@pytest.fixture(autouse=True)
def _clean_markers():
    """The legacy cancel registry is process-global; keep tests independent."""

    with legacy._lock:
        legacy._cancelled_turns.clear()
    yield
    with legacy._lock:
        legacy._cancelled_turns.clear()


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_runtime_cancellation(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.TRUSTED
    assert item.slot is SlotName.HANDOFF_ASSIGNMENT and m.attaches_to == (SlotName.HANDOFF_ASSIGNMENT,)
    assert m.provides_operations == () and m.requires_operations == () and m.policy_actions == () and m.hooks == ()
    assert isinstance(item.provider, CancellationModule)

    d = _describe(registry)
    assert d["kind"] == "T" and d["slot"] == "handoff.assignment" and d["enabled"] is True and d["source"] == "builtin"
    assert d["name"] == "中断与恢复"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["provides"] == [] and d["requires"] == [] and d["hooks"] == [] and d["policy_actions"] == []
    assert d["guarded"] is True
    assert d["guarded"] or not d["policy_actions"]


# --------------------------------------------------------------------------- 2. placement

def test_placement_runtime_cancellation(registry):
    m = _placed(registry)
    assert m["placement"] == {"big_id": "interaction", "sub_id": "interaction.resume", "source": "taxonomy"}
    assert m["switchable"] is False
    assert m["movable"] is True and m["slot"] not in FIXED_SLOTS


# --------------------------------------------------------------------------- 3. disable

def test_disable_runtime_cancellation(registry):
    """Not switchable: the admin API refuses to stop it (``是平台核心组成部分，不能停用``)."""

    d = _describe(registry)
    assert d["kind"] == "T" and d["switchable"] is False
    assert d["slot"] not in {"runtime.engine", "security.pep"}

    class Disabled(FakeSettings):
        dsh_disabled_modules = MODULE_ID

    reg = discover_and_install(ModuleRegistry(), Disabled())
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    assert reg.get(MODULE_ID) is not None and reg.get(MODULE_ID).enabled is False
    assert _describe(reg)["switchable"] is False


# --------------------------------------------------------------------------- 4. provider

def test_provider_runtime_cancellation_unknown_turn_is_not_cancelled(module, db):
    mod = module(MODULE_ID).provider
    assert mod.is_cancelled("s1", "turn1") is False
    assert mod.is_cancelled("s1", "turn1", db=db) is False
    # empty identifiers are never cancelled
    assert mod.is_cancelled("", "turn1") is False and mod.is_cancelled("s1", "") is False


def test_provider_runtime_cancellation_sees_in_process_marker(module):
    mod = module(MODULE_ID).provider
    legacy.cancel_chat_turn("s1", "turn1")
    assert mod.is_cancelled("s1", "turn1") is True
    # scoped to the exact (session, turn)
    assert mod.is_cancelled("s1", "turn2") is False and mod.is_cancelled("s2", "turn1") is False
    legacy.clear_chat_turn_cancelled("s1", "turn1")
    assert mod.is_cancelled("s1", "turn1") is False


def test_provider_runtime_cancellation_sees_durable_event_and_receipt(module, db):
    mod = module(MODULE_ID).provider
    # a durable stream_cancelled event is the restart-safe source of truth
    db.add(AgentEvent(tenant_id="t1", session_id="s1", event_type="stream_cancelled", payload_json={"turn_id": "turn9"}))
    db.commit()
    assert mod.is_cancelled("s1", "turn9") is False          # no db → in-process only
    assert mod.is_cancelled("s1", "turn9", db=db) is True
    assert mod.is_cancelled("s1", "other", db=db) is False
    # once the claimed turn records a terminal status, the receipt wins over the event
    db.add(HarnessTurnRecord(tenant_id="t1", session_id="s1", client_turn_id="turn9", request_digest="d9", lease_owner="w1", lease_expires_at=utc_now(), status="completed"))
    db.commit()
    assert mod.is_cancelled("s1", "turn9", db=db) is False
    db.add(HarnessTurnRecord(tenant_id="t1", session_id="s1", client_turn_id="turn10", request_digest="d10", lease_owner="w1", lease_expires_at=utc_now(), status="cancelled"))
    db.commit()
    assert mod.is_cancelled("s1", "turn10", db=db) is True


# --------------------------------------------------------------------------- 5. events / pep

def test_events_or_pep_runtime_cancellation(module, guard, security_ctx):
    """Cancellation declares no policy actions — it is a runtime signal, not a resource access."""

    m = module(MODULE_ID).manifest
    assert m.policy_actions == () and m.provides_operations == ()
    # nothing of its own to map; the guard is still constructible for the host that owns it
    assert guard(MODULE_ID).module_id == MODULE_ID
    assert not any(op.startswith("runtime.cancel") for op in DEFAULT_ACTION_MAP)
