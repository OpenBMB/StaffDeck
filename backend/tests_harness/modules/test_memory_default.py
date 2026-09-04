"""Per-module tests for ``memory.default`` (记忆, kind T, slot runtime.memory) and the seam that
makes memory pluggable on the Harness v3 path.

What "pluggable" must mean here, and what each test pins:
1. The module is registered, guarded, switchable, placed under 能力 → 记忆 in the admin tree.
2. The built-in provider's ``recall`` returns exactly what ``MemoryService.context_memories``
   returned, in ``memory_read`` shape (behaviour unchanged by default).
3. ``ProviderMemoryFacade`` speaks the surface the v2 engine reads (``context_memories`` →
   rows ``memory_read`` accepts), so the v2 turn skeleton needs no change.
4. A *different* provider installed into ``runtime.memory`` is what the turn recalls.
5. With the module disabled, recall is empty and the turn runs without memory (not an error).
6. ``HarnessV3Engine.run`` installs the facade for the turn and restores ``owner.memory`` after.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.models import MemoryRecord, utc_now
from app.memory.service import memory_read
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.memory import MemoryDefaultModule, ProviderMemoryFacade, resolve_memory_provider
from staffdeck_harness.modules import registry as registry_mod
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "memory.default"


def _placed(registry, module_id: str = MODULE_ID) -> dict:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return {"big": big["id"], "sub": sub["id"], **m}
    raise AssertionError(f"{module_id} not placed")


# --------------------------------------------------------------------------- 1. manifest / placement

def test_manifest_memory_default(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert item.slot is SlotName.RUNTIME_MEMORY and m.kind is ModuleKind.TRUSTED
    assert set(m.provides_operations) == {"memory.read/v1", "memory.write/v1"}
    assert set(m.policy_actions) == {"memory.read/v1", "memory.write/v1"}
    assert m.metadata.get("switchable") is True, "memory must be a switch the operator can turn off"
    assert SlotName.RUNTIME_MEMORY in registry._guarded_slots
    placed = _placed(registry)
    assert (placed["big"], placed["sub"]) == ("capability", "capability.memory")
    assert resolve_memory_provider(registry=registry) is item.provider


# --------------------------------------------------------------------------- 2. builtin recall == MemoryService

def _mem(db, **kw):
    row = MemoryRecord(tenant_id="t1", user_id="u1", username="alice", session_id="s0", kind=kw.pop("kind", "preference"), content=kw.pop("content", "喜欢简短回答"), importance=0.7, metadata_json={"agent_id": kw.pop("agent_id", "a1"), "key": "style"}, created_at=utc_now(), updated_at=utc_now())
    db.add(row)
    db.commit()
    return row


def test_builtin_recall_matches_memory_service(db):
    a = _mem(db, content="喜欢简短回答")
    _mem(db, content="别的员工的记忆", agent_id="a9")
    out = MemoryDefaultModule().recall(db, "t1", "u1", "a1")
    from app.memory.service import MemoryService

    expected = [memory_read(r) for r in MemoryService(db).context_memories("t1", "u1", agent_id="a1")]
    assert out == expected
    assert [m["content"] for m in out] == [a.content], "agent scoping is the service's, unchanged"


# --------------------------------------------------------------------------- 3. facade speaks the v2 surface

def test_facade_rows_round_trip_through_memory_read(db):
    _mem(db, content="喜欢简短回答")
    facade = ProviderMemoryFacade(db, MemoryDefaultModule())
    rows = facade.context_memories("t1", "u1", agent_id="a1")
    assert rows and memory_read(rows[0])["content"] == "喜欢简短回答"
    assert memory_read(rows[0])["kind"] == "preference" and memory_read(rows[0])["metadata"]["key"] == "style"
    assert facade.recall("t1", "u1", query="ignored", agent_id="a1")[0].content == "喜欢简短回答"
    assert facade.context_memories("t1", "", agent_id="a1") == [], "no user → no recall"


# --------------------------------------------------------------------------- 4. a swapped provider is what the turn reads

class _VectorMemory:
    """A stand-in for an external memory service: returns its own rows, never touches the DB."""

    module_id = "memory.vector"

    def recall(self, db, tenant_id, user_id, agent_id=None, *, session_id=None, query=""):
        return [{"id": "v1", "tenant_id": tenant_id, "user_id": user_id, "kind": "fact", "content": f"vector:{user_id}:{agent_id}", "importance": 0.9, "metadata": {"source": "vector"}}]


def _registry_with(provider, *, enabled=True, settings=None):
    from staffdeck_harness.contracts.manifest import ModuleManifest
    from tests_harness.modules.conftest import FakeSettings

    reg = discover_and_install(ModuleRegistry(), settings or FakeSettings())
    reg.set_enabled(MODULE_ID, False)
    reg.install(ModuleManifest(module_id="memory.vector", name="向量记忆", version="0.1.0", kind=ModuleKind.CODE, contract_version="v1", attaches_to=(SlotName.RUNTIME_MEMORY,), provides_operations=("memory.read/v1",), policy_actions=("memory.read/v1",)), provider, slot=SlotName.RUNTIME_MEMORY, enabled=enabled)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    return reg


def test_swapped_provider_is_what_the_turn_recalls(db, monkeypatch):
    _mem(db, content="DB 里的记忆")  # would be recalled by the builtin; must NOT appear
    reg = _registry_with(_VectorMemory())
    monkeypatch.setattr(registry_mod, "_active", reg)
    provider = resolve_memory_provider()
    assert type(provider).__name__ == "_VectorMemory"
    facade = ProviderMemoryFacade(db, provider)
    out = [memory_read(r) for r in facade.context_memories("t1", "u1", agent_id="a1")]
    assert [m["content"] for m in out] == ["vector:u1:a1"]
    assert out[0]["metadata"] == {"source": "vector"}


# --------------------------------------------------------------------------- 5. disabled → no memory, not an error

def test_disabled_memory_recalls_nothing(db, monkeypatch):
    _mem(db, content="DB 里的记忆")
    from tests_harness.modules.conftest import FakeSettings

    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    reg.set_enabled(MODULE_ID, False)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    assert resolve_memory_provider() is None
    assert ProviderMemoryFacade(db, None).context_memories("t1", "u1", agent_id="a1") == []


def test_no_registry_means_no_memory(monkeypatch):
    monkeypatch.setattr(registry_mod, "_active", None)
    assert resolve_memory_provider() is None


# --------------------------------------------------------------------------- 6. the engine installs the facade for the turn only

def test_harness_v3_engine_swaps_owner_memory_for_the_turn(db, monkeypatch, profile):
    from staffdeck_harness.bridge import engine_host

    reg = _registry_with(_VectorMemory())
    monkeypatch.setattr(registry_mod, "_active", reg)
    original_memory = object()
    seen = {}

    class _Owner:
        def __init__(self):
            self.db = db
            self.events = SimpleNamespace(execution_engine=None, record=lambda *a, **k: None)
            self.memory = original_memory
            self.response_generator = object()

    def fake_run(self, request):
        seen["memory_during_turn"] = self.owner.memory
        seen["rows"] = [memory_read(r) for r in self.owner.memory.context_memories("t1", "u1", agent_id="a1")]
        return "done"

    monkeypatch.setattr(engine_host.HarnessV2Engine, "run", fake_run)
    engine = engine_host.HarnessV3Engine(_Owner(), runtime=SimpleNamespace(worker_config=None, settings=None, release_process=lambda p: None), profile=profile)
    assert engine.run(SimpleNamespace(tenant_id="t1", session_id="s1", agent_id="a1", user_id="u1", attachments=[])) == "done"
    assert isinstance(seen["memory_during_turn"], ProviderMemoryFacade)
    assert [m["content"] for m in seen["rows"]] == ["vector:u1:a1"], "the v2 skeleton read memory through the provider"
    assert engine.owner.memory is original_memory, "restored after the turn"


@pytest.mark.parametrize("enabled", [True, False])
def test_engine_tolerates_disabled_memory(db, monkeypatch, profile, enabled):
    from staffdeck_harness.bridge import engine_host

    reg = _registry_with(_VectorMemory(), enabled=enabled)
    monkeypatch.setattr(registry_mod, "_active", reg)
    seen = {}

    owner = SimpleNamespace(db=db, events=SimpleNamespace(execution_engine=None, record=lambda *a, **k: None), memory=object(), response_generator=object())

    monkeypatch.setattr(engine_host.HarnessV2Engine, "run", lambda self, r: seen.setdefault("n", len(self.owner.memory.context_memories("t1", "u1", agent_id="a1"))))
    engine_host.HarnessV3Engine(owner, runtime=SimpleNamespace(worker_config=None, settings=None, release_process=lambda p: None), profile=profile).run(SimpleNamespace(tenant_id="t1", session_id="s1", agent_id="a1", user_id="u1", attachments=[]))
    assert seen["n"] == (1 if enabled else 0)
