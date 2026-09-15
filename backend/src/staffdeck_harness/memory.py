"""Engine-independent memory Host and built-in adapter.

Recall, capture, listing and clearing share the selected runtime.memory provider.
Staff/SOP bindings choose the provider; the public MemoryCall contains only data.
Database access and the legacy async capture job stay behind a scoped call_local
service. TurnCoordinator owns the facade; AgentLoop collaborators are never patched.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from app.memory.service import MemoryService
from staffdeck_harness.contracts.memory import MemoryCall, MemoryContext, MemoryProvider
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.modules.registry import ModuleRegistry


class MemoryDefaultModule:
    module_id = "memory.default"
    name = "memory.default"

    def __init__(self, service: MemoryService | None = None):
        self._service = service

    def service(self, db: Any) -> MemoryService:
        return self._service or MemoryService(db)

    def invoke(self, context: MemoryContext, call: MemoryCall) -> Any:
        return context.call_local()


class ProviderMemoryFacade:
    """Engine-independent memory Host. Only this adapter holds database/domain objects."""

    def __init__(self, db: Any, provider: MemoryProvider | None, *, profile: Any = None, config: Mapping[str, Any] | None = None, registry: Any = None, source_context: Any = None):
        self.db = db
        self.provider = provider
        self.profile = profile
        self.config = dict(config or {})
        from staffdeck_harness.modules.registry import peek_registry

        self.registry = registry or peek_registry()
        self.source_context = source_context

    def authorize(self, tenant_id: str, user_id: str, operation: str, *, optional: bool = False) -> bool:
        from app.db.models import User
        from staffdeck_harness.contracts.security import ResourceRef
        from staffdeck_harness.security.profile import Guard, peek_profile
        from staffdeck_harness.security.oss_local import build_oss_local_profile
        from staffdeck_harness.contracts.errors import PermissionDenied

        profile = self.profile or peek_profile() or build_oss_local_profile()
        user = self.db.get(User, user_id)
        if user is None or user.tenant_id != tenant_id:
            raise PermissionDenied("memory principal unavailable")
        if self.registry is not None:
            from staffdeck_harness.composition.sources import resolve_source
            from staffdeck_harness.contracts.sources import SourceContext

            context = self.source_context or SourceContext(tenant_id, None, user_id=user_id)
            if (context.tenant_id, context.user_id) != (tenant_id, user_id):
                raise PermissionDenied("memory source identity mismatch")
            ctx = resolve_source(self.registry, SlotName.IDENTITY_SOURCE, self.db).resolve(context, profile.identity)
        else:
            ctx = profile.identity.from_user(user)
        if (ctx.tenant_id, ctx.actor_user_id or ctx.principal_id) != (tenant_id, user_id):
            raise PermissionDenied("memory principal mismatch")
        scope = ctx.execution.channel_scope if ctx.execution is not None else ctx.attributes.get("channel_scope")
        if scope is not None:
            # The current provider contract stores account-wide personal memory,
            # not channel/session-scoped memory. Admission to a published employee
            # must never grant that broader scope (including background capture).
            from staffdeck_harness.contracts.security import PolicyActionMapper

            Guard("memory", profile, PolicyActionMapper({"memory.context/v1": ("read", "session")})).require(
                ctx, "memory.context/v1", ResourceRef(type="session", id=scope.session_id,
                    tenant_id=tenant_id, attributes={"user_id": user_id}))
            if optional:
                return False
            raise PermissionDenied("channel scope does not grant personal memory access")
        Guard("memory", profile).require(ctx, operation, ResourceRef(type="session", id=f"memory:{user_id}", tenant_id=tenant_id, attributes={"user_id": user_id}))
        return True

    def call(self, request: MemoryCall, *, actor_id: str, local: Callable[[], Any]) -> Any:
        if self.source_context is not None and (
                request.agent_id != self.source_context.staff_id
                or request.session_id is not None and request.session_id != self.source_context.session_id):
            from staffdeck_harness.contracts.errors import PermissionDenied
            raise PermissionDenied("memory request scope mismatch")
        if not self.authorize(request.tenant_id, actor_id,
                "memory.write/v1" if request.operation in {"capture", "clear"} else "memory.read/v1",
                optional=request.operation in {"recall", "capture"}):
            return []
        if self.provider is None:
            return {"deleted": 0} if request.operation == "clear" else []
        from types import MappingProxyType

        from contextlib import nullcontext

        with self.registry.work_lease() if self.registry else nullcontext():
            return self.provider.invoke(MemoryContext(MappingProxyType(self.config), local), request)

    def context_memories(self, tenant_id: str, user_id: str, *, agent_id: str | None = None) -> list[Any]:
        if self.provider is None or not user_id:
            return []
        from app.memory.service import memory_read

        def local():
            service = self.provider.service(self.db) if isinstance(self.provider, MemoryDefaultModule) else MemoryService(self.db)
            return [memory_read(row) for row in service.context_memories(tenant_id, user_id, agent_id=agent_id)]
        result = self.call(MemoryCall("recall", tenant_id, user_id, agent_id), actor_id=user_id, local=local)
        return [_RowLike(d) for d in result]

    def recall(self, tenant_id: str, user_id: str, query: str = "", limit: int | None = None, agent_id: str | None = None) -> list[Any]:
        return self.context_memories(tenant_id, user_id, agent_id=agent_id)

    def bind_capture(self, events: Any, legacy_enqueue: Callable[..., list[dict[str, Any]]]) -> Callable[..., list[dict[str, Any]]]:
        def enqueue(request: Any, session: Any, step_result: Any, tool_result: Any, model_config: Any) -> list[dict[str, Any]]:
            if self.provider is None or not getattr(request, "user_id", None):
                return []
            # Deliberately exclude DB handles, model credentials and callbacks from the request.
            call = MemoryCall("capture", request.tenant_id, request.user_id, session.agent_id, session.id,
                              payload={"message": str(getattr(request, "message", "") or ""),
                                       "reply": str(getattr(step_result, "reply", "") or "")})
            try:
                out = self.call(call, actor_id=request.user_id, local=lambda: legacy_enqueue(request, session, step_result, tool_result, model_config))
            except Exception as exc:
                record = getattr(events, "record", None)
                if callable(record):
                    record(call.tenant_id, call.session_id, "memory_error", {"message": str(exc), "provider": getattr(self.provider, "module_id", type(self.provider).__name__)})
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


def resolve_memory_provider(*, registry: ModuleRegistry | None = None, snapshot: Any = None, sop_id: str | None = None) -> MemoryProvider | None:
    """The enabled ``runtime.memory`` provider, if any. ``None`` means memory is disabled."""

    from staffdeck_harness.modules.registry import peek_registry

    reg = registry or peek_registry()
    if reg is None:
        return None
    candidates = reg.selected(SlotName.RUNTIME_MEMORY, snapshot, sop_id=sop_id)
    if len(candidates) > 1:
        from staffdeck_harness.contracts.errors import ContractIncompatible

        raise ContractIncompatible("multiple memory providers require an explicit Staff/SOP binding")
    installed = candidates[0] if candidates else None
    return installed.provider if installed is not None else None


def for_staff(db: Any, tenant_id: str, agent_id: str | None, *, sop_id: str | None = None) -> ProviderMemoryFacade:
    from staffdeck_harness.modules.registry import peek_registry
    from staffdeck_harness.composition.compiler import CompositionCompiler
    from staffdeck_harness.composition.sources import load_composition
    from staffdeck_harness.contracts.sources import SourceContext
    from staffdeck_harness.contracts.errors import PermissionDenied

    registry = peek_registry()
    if registry is None:
        return ProviderMemoryFacade(db, MemoryDefaultModule())
    # Own historical memory must remain deletable after a Staff was removed. In that case
    # there is no Staff-specific binding; the deployment provider still receives agent_id.
    snapshot = None
    if agent_id:
        try:
            snapshot = CompositionCompiler().compile(load_composition(registry, db, SourceContext(tenant_id, agent_id)), strict=False)
        except PermissionDenied:
            pass  # a deleted Staff must not make historical own memory undeletable
    return ProviderMemoryFacade(db, resolve_memory_provider(registry=registry, snapshot=snapshot, sop_id=sop_id), config=memory_config(registry, snapshot, sop_id), registry=registry)


def memory_config(registry: ModuleRegistry, snapshot: Any = None, sop_id: str | None = None) -> dict[str, Any]:
    selected = registry.selected(SlotName.RUNTIME_MEMORY, snapshot, sop_id=sop_id)
    binding = snapshot.module_binding(SlotName.RUNTIME_MEMORY, sop_id=sop_id) if snapshot else None
    return {**(dict(selected[0].config) if selected else {}), **(dict(binding.metadata) if binding else {})}


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
