"""Memory provider (``memory.read/v1`` / ``memory.write/v1``), pluggable via ``runtime.memory``.

The turn skeleton used to read memory straight from ``AgentLoop.memory`` (a ``MemoryService``)
and write it via a fixed background job. This module turns that into a registry contract so a
deployment can swap recall/capture, or disable memory entirely:

- ``MemoryDefaultModule`` (module id ``memory.default``) is the built-in provider. Its
  ``recall`` and ``capture`` wrap the existing ``MemoryService`` (for recall) and the legacy
  capture job (for the async write fallback), so behaviour is unchanged unless someone installs
  a different provider into ``runtime.memory``.
- ``AgentLoop`` stays untouched. ``HarnessV3Engine`` resolves the ``runtime.memory`` provider at
  turn start and re-points ``owner.memory`` at a ``ProviderMemoryFacade`` that delegates to it;
  the v2 engine keeps reading ``owner.memory``, so both engines pick up the provider the moment
  the v3 engine is the one running the turn. The facade is per-turn and only installed for a
  turn that runs on the v3 path.

Which half is a real provider hook today, and which is a thin default:
- ``recall`` is the real seam. The provider returns ``list[dict]`` in ``memory_read`` shape;
  the v3 pre_step ``memory.recall`` hook renders those into the step prompt exactly as before.
- ``capture`` is the async tail: the engine calls ``_enqueue_memory_capture`` which schedules a
  background job. The provider's ``capture`` is offered as a synchronous override for providers
  that keep their own store; the built-in provider falls back to … nothing (it does not own the
  background queue), so a swapping provider owns both halves. Drivers that want to keep the
  legacy job can leave ``capture`` as the shipped default and only replace recall.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.memory.service import MemoryService
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.modules.registry import ModuleRegistry


class MemoryProvider:
    """The SPI a ``runtime.memory`` module implements."""

    def recall(self, db: Any, tenant_id: str, user_id: str, agent_id: str | None = None, *, session_id: str | None = None, query: str = "") -> list[dict[str, Any]]:
        raise NotImplementedError

    def capture(self, db: Any, tenant_id: str, user_id: str, agent_id: str | None, *, session_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Synchronous write. The built-in provider ignores it (the legacy path is async); a
        swapping provider owns its own write semantics here."""
        return []


class MemoryDefaultModule:
    module_id = "memory.default"
    name = "memory.default"

    def __init__(self, service: MemoryService | None = None):
        self._service = service

    def service(self, db: Any) -> MemoryService:
        return self._service or MemoryService(db)

    def recall(self, db: Any, tenant_id: str, user_id: str, agent_id: str | None = None, *, session_id: str | None = None, query: str = "") -> list[dict[str, Any]]:
        from app.memory.service import memory_read

        rows = self.service(db).context_memories(tenant_id, user_id, agent_id=agent_id)
        return [memory_read(r) for r in rows]


class ProviderMemoryFacade:
    """What ``owner.memory`` becomes for a Harness v3 turn: the same ``context_memories`` surface
    the v2 engine reads, answered by the registry's ``runtime.memory`` provider. With no provider
    (memory disabled) it recalls nothing, so the turn simply runs without memory context."""

    def __init__(self, db: Any, provider: MemoryProvider | None):
        self.db = db
        self.provider = provider

    def context_memories(self, tenant_id: str, user_id: str, *, agent_id: str | None = None) -> list[Any]:
        if self.provider is None or not user_id:
            return []
        return [_RowLike(d) for d in self.provider.recall(self.db, tenant_id, user_id, agent_id)]

    def recall(self, tenant_id: str, user_id: str, query: str = "", limit: int | None = None, agent_id: str | None = None) -> list[Any]:
        return self.context_memories(tenant_id, user_id, agent_id=agent_id)


class _RowLike:
    """``memory_read`` accepts a MemoryRecord; provide the same attributes from a dict so the v2
    engine's ``[memory_read(row) for row in owner.memory.context_memories(...)]`` keeps working."""

    def __init__(self, d: Mapping[str, Any]):
        from datetime import datetime, timezone

        self.id = d.get("id")
        self.tenant_id = d.get("tenant_id")
        self.user_id = d.get("user_id")
        self.username = d.get("username")
        self.session_id = d.get("session_id")
        self.kind = d.get("kind", "conversation")
        self.content = str(d.get("content") or "")
        self.importance = float(d.get("importance") or 0.5)
        self.metadata_json = dict(d.get("metadata") or {})
        now = datetime.now(timezone.utc)
        self.created_at = _dt(d.get("created_at"), now)
        self.updated_at = _dt(d.get("updated_at"), now)


def _dt(value: Any, default: Any) -> Any:
    from datetime import datetime

    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return default
    return default


def resolve_memory_provider(*, registry: ModuleRegistry | None = None) -> MemoryProvider | None:
    """The enabled ``runtime.memory`` provider, if any. ``None`` means memory is disabled."""

    from staffdeck_harness.modules.registry import peek_registry

    reg = registry or peek_registry()
    if reg is None:
        return None
    installed = reg.provider(SlotName.RUNTIME_MEMORY)
    return installed.provider if installed is not None else None


def install(registry: ModuleRegistry, settings: Any = None, *, enabled: bool = True) -> None:
    registry.install(
        _manifest(summary="内置记忆：按用户与员工召回已有记忆，写入走后台任务；关闭则本轮不带记忆。", enabled=enabled),
        MemoryDefaultModule(),
        slot=SlotName.RUNTIME_MEMORY,
        enabled=enabled,
    )


def _manifest(*, summary: str, enabled: bool = True):
    from staffdeck_harness.contracts.manifest import ModuleKind, ModuleManifest

    return ModuleManifest(
        module_id="memory.default", name="内置记忆", version="1.0.0", kind=ModuleKind.TRUSTED,
        contract_version="v1", attaches_to=(SlotName.RUNTIME_MEMORY,),
        provides_operations=("memory.read/v1", "memory.write/v1"),
        policy_actions=("memory.read/v1", "memory.write/v1"),
        metadata={"summary": summary, "switchable": True},
    )
