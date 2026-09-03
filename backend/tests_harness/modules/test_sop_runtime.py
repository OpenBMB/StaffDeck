"""Module tests for ``sop.runtime`` (流程执行推进, kind T, slot sop.slot.control).

Provider: ``staffdeck_harness.modules.kernel.SopRuntimeModule`` — hands out the
legacy ``TaskFrameStore`` (TaskFrame / agent-loop CAS state machine).
"""

from __future__ import annotations

import re

import pytest

from app.core.task_frame_store import TaskFrameClaimConflict, TaskFrameStore
from app.db.models import HarnessAgentLoopRecord, HarnessTaskFrameRecord
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.modules.kernel import SopRuntimeModule
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "sop.runtime"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def _placed(registry, module_id: str = MODULE_ID) -> dict:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return m
    raise AssertionError(f"{module_id} not found in taxonomy tree")


@pytest.fixture
def frame(db):
    row = HarnessTaskFrameRecord(tenant_id="t1", session_id="s1", source_turn_id="turn1", task_id="task1", kind="sop", skill_id="skill_1", status="queued")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_sop_runtime(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind.value == "T"
    assert item.slot is SlotName.SOP_SLOT_CONTROL
    assert m.attaches_to == (SlotName.SOP_SLOT_CONTROL,)
    assert m.provides_operations == ()
    assert m.requires_operations == ()
    assert m.policy_actions == ()
    assert m.hooks == ()
    assert isinstance(item.provider, SopRuntimeModule)
    assert item.provider.module_id == MODULE_ID

    d = _described(registry)
    assert d["name"] == "流程执行推进"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["enabled"] is True
    assert d["policy_actions"] == []
    assert d["guarded"] is True  # shares the PEP-bound sop.slot.control host
    assert "switchable" not in d["metadata"]


def test_manifest_guarded_by_builtin_registration(settings):
    reg = discover_and_install(ModuleRegistry(), settings)
    d = _described(reg)
    assert d["guarded"] is True and d["policy_actions"] == []


# --------------------------------------------------------------------------- 2. placement

def test_placement_sop_runtime(registry):
    d = _described(registry)
    assert d["switchable"] is False  # trusted service without a ``switchable`` marker
    placed = _placed(registry)
    assert placed["placement"] == {"big_id": "sop", "sub_id": "sop.runtime", "source": "taxonomy"}
    assert placed["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_sop_runtime_not_switchable(registry):
    d = _described(registry)
    assert d["kind"] == "T"
    assert d["switchable"] is False


# --------------------------------------------------------------------------- 4. provider

def test_provider_sop_runtime_store_returns_task_frame_store(module, db):
    store = module(MODULE_ID).provider.store(db)
    assert isinstance(store, TaskFrameStore)
    assert store.db is db


def test_provider_sop_runtime_agent_loop_round_trip(module, db, frame):
    store = module(MODULE_ID).provider.store(db)
    assert store.agent_loop_checkpoint(frame) == {}
    loop = store.ensure_agent_loop(frame)
    assert isinstance(loop, HarnessAgentLoopRecord)
    assert loop.loop_key == f"sop:{frame.id}" and loop.kind == "sop" and loop.status == "active"
    assert frame.agent_loop_id == loop.id
    # idempotent: a second call reuses the same durable loop
    again = store.ensure_agent_loop(frame)
    assert again.id == loop.id

    store.save_agent_loop_checkpoint(loop, {"step": 2}, status="suspended", last_run_id="run1")
    db.commit()
    assert store.agent_loop_checkpoint(frame) == {"step": 2}
    assert loop.finished_at is None and loop.last_run_id == "run1"

    store.finish_agent_loop_for_frame(frame, result_status="completed", checkpoint={"step": 3}, last_run_id="run2")
    db.commit()
    assert loop.status == "completed" and loop.finished_at is not None
    assert store.agent_loop_checkpoint(frame) == {"step": 3}


def test_provider_sop_runtime_mark_running_is_cas(module, db, frame):
    store = module(MODULE_ID).provider.store(db)
    version = frame.state_version
    store.mark_running(frame)
    db.commit()
    assert frame.status == "running" and frame.attempt_no == 1 and frame.lease_owner
    assert frame.state_version == version + 1
    # already claimed -> the compare-and-swap refuses a second claim
    with pytest.raises(TaskFrameClaimConflict):
        store.mark_running(frame)


# --------------------------------------------------------------------------- 5. PEP

def test_pep_sop_runtime_host_guard_denies_cross_tenant(guard, security_ctx, module):
    """Kernel/trusted entry without own policy actions; the sop.* actions of its host map through DEFAULT_ACTION_MAP."""

    assert module(MODULE_ID).manifest.policy_actions == ()
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    assert mapper.map("sop.advance/v1") == ("advance", "sop")
    assert mapper.map("sop.resume/v1") == ("resume", "sop")
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="sop", id="skill_1", tenant_id="t2")
    for op in ("sop.execute/v1", "sop.advance/v1", "sop.resume/v1"):
        with pytest.raises(PermissionDenied):
            g.require(security_ctx(), op, foreign)
