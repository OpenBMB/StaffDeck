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

Both halves are provider seams:
- ``recall`` returns ``list[dict]`` in ``memory_read`` shape; the v3 pre_step ``memory.recall``
  hook renders those into the step prompt exactly as before.
- ``capture(ctx)`` is called where the v2 skeleton used to call
  ``owner._enqueue_memory_capture(...)`` (end of a visible turn). ``ctx.legacy_enqueue()`` is
  that original method, captured for the turn, so the built-in provider keeps the legacy async
  background job — events, commit and all — without replicating it. A swapping provider writes
  to its own store synchronously (and may still call ``legacy_enqueue`` if it wants both). With
  the module disabled nothing is written and nothing fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.memory.service import MemoryService
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.modules.registry import ModuleRegistry


@dataclass
class CaptureContext:
    """Everything the end-of-turn write sees. ``legacy_enqueue`` is the original
    ``AgentLoop._enqueue_memory_capture`` bound for this call: invoking it schedules the legacy
    background capture job (and records its events / commits) exactly as before."""

    db: Any
    events: Any
    request: Any
    session: Any
    step_result: Any
    tool_result: Any
    model_config: Any
    legacy_enqueue: Callable[[], list[dict[str, Any]]]

    @property
    def tenant_id(self) -> str:
        return str(getattr(self.request, "tenant_id", "") or "")

    @property
    def user_id(self) -> str:
        return str(getattr(self.request, "user_id", "") or "")

    @property
    def agent_id(self) -> str | None:
        return getattr(self.session, "agent_id", None)

    @property
    def session_id(self) -> str:
        return str(getattr(self.session, "id", "") or "")


class MemoryProvider:
    """The SPI a ``runtime.memory`` module implements."""

    def recall(self, db: Any, tenant_id: str, user_id: str, agent_id: str | None = None, *, session_id: str | None = None, query: str = "") -> list[dict[str, Any]]:
        raise NotImplementedError

    def capture(self, ctx: CaptureContext) -> list[dict[str, Any]]:
        """Persist what this turn taught us about the user. Return receipts (dicts) for the trace.

        Default: nothing. The built-in provider returns ``ctx.legacy_enqueue()``; a provider with
        its own store writes synchronously here.
        """
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

    def capture(self, ctx: CaptureContext) -> list[dict[str, Any]]:
        # The legacy path: schedule the background capture job. Same events, same commit.
        return ctx.legacy_enqueue()


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

    # -- write seam ------------------------------------------------------------------------

    def bind_capture(self, events: Any, legacy_enqueue: Callable[..., list[dict[str, Any]]]) -> Callable[..., list[dict[str, Any]]]:
        """Return the callable that replaces ``owner._enqueue_memory_capture`` for this turn.

        Same signature as the original: ``(request, session, step_result, tool_result,
        model_config)``. Routes to ``provider.capture``; the built-in provider forwards to
        ``legacy_enqueue``. A provider that raises records ``memory_error`` and the turn goes on,
        which is what the legacy method did for a failed enqueue.
        """

        def enqueue(request: Any, session: Any, step_result: Any, tool_result: Any, model_config: Any) -> list[dict[str, Any]]:
            if self.provider is None or not getattr(request, "user_id", None):
                return []
            ctx = CaptureContext(
                db=self.db, events=events, request=request, session=session, step_result=step_result, tool_result=tool_result, model_config=model_config,
                legacy_enqueue=lambda: legacy_enqueue(request, session, step_result, tool_result, model_config),
            )
            try:
                out = self.provider.capture(ctx)
            except Exception as exc:  # noqa: BLE001 - a memory write must never take the turn down
                record = getattr(events, "record", None)
                if callable(record):
                    record(ctx.tenant_id, ctx.session_id, "memory_error", {"message": str(exc), "provider": getattr(self.provider, "module_id", type(self.provider).__name__)})
                return []
            return list(out or [])

        return enqueue


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
        _manifest(summary="内置记忆：按用户与员工召回已有记忆，对话后由后台任务提炼并写入；关闭则本轮既不带记忆也不写入。", enabled=enabled),
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
