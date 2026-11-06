"""One deployment service factory for every runtime worker, trace and cancellation read."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from sqlmodel import Session

from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.manifest import SlotName


def runtime_models():
    """Canonical shared Store models; storage adapters change binds, not Store algorithms."""
    from app.db import models
    names = {"ChatSession", "Message", "AgentEvent", "MemoryRecord", "HumanHandoffRequest", "Team", "TeamMember", "EvolutionProposal",
             "MessageFeedback", "SkillFeedback", "ScheduledTaskRun", "APIJob", "APIJobEvent",
             "ExternalSessionBinding", "WebhookDelivery", "APIIdempotencyRecord", "WeChatKfAccount"}
    prefixes = ("Harness", "TeamRun", "TeamTask", "TeamWake", "TeamBlackboard", "Channel", "ExternalBusinessTask")
    return {name: value for name, value in vars(models).items()
            if isinstance(value, type) and hasattr(value, "__table__") and
            (name in names or name.startswith(prefixes))}


def maintenance_sessions():
    """Deployment-owned tenant transactions for sweepers, never a guessed user identity."""
    from app.db import engine
    from staffdeck_harness.modules.registry import peek_registry
    from contextlib import nullcontext
    registry = peek_registry()
    with registry.work_lease() if registry else nullcontext():
        with Session(engine) as control:
            module = registry.provider(SlotName.RUNTIME_SERVICES).provider if registry else LocalRuntimeServicesModule()
            yield from module.maintenance_sessions(control)


class LocalRuntimeServices:
    namespace = "oss-local"
    models: dict[str, type] = {}

    def __init__(self, db):
        self.bind = db.get_bind()
        self.info = dict(getattr(db, "info", {}))
        self.registry = self.info.get("staffdeck_registry")

    @contextmanager
    def session(self, identity=None, *, purpose="runtime"):
        with Session(self.bind) as db:
            db.info.update(self.info)
            db.info["staffdeck_runtime_services"] = self
            yield db


class LocalRuntimeServicesModule:
    def build(self, db):
        return LocalRuntimeServices(db)

    def background_services(self, db):
        return self.build(db)

    def maintenance_sessions(self, db):
        with self.build(db).session(None, purpose="recovery") as session:
            yield session


def recover_runtime_runs(*, startup=False):
    """Run the same recovery Store once per module-owned tenant transaction."""
    from app.db import engine
    from app.core.harness_recovery import recover_orphan_harness_runs
    from staffdeck_harness.modules.registry import peek_registry
    registry = peek_registry()
    if registry is None:
        return
    item = registry.provider(SlotName.RUNTIME_SERVICES)
    if item is None:
        return
    with registry.turn_lease(), Session(engine) as control_db:
        for db in item.provider.maintenance_sessions(control_db):
            recover_orphan_harness_runs(db, startup=startup)


def runtime_services(db, registry=None):
    pinned = getattr(db, "info", {}).get("staffdeck_runtime_services")
    if pinned is not None:
        return pinned
    if registry is None:
        from staffdeck_harness.modules.registry import peek_registry
        registry = getattr(db, "info", {}).get("staffdeck_registry") or peek_registry()
    installed = registry.provider(SlotName.RUNTIME_SERVICES) if registry else None
    if installed is None:
        # Unassembled standalone helpers preserve the existing local behavior.
        if registry and registry.get("engine.harness_v3"):
            raise ModuleSdkError("运行数据服务未装配", code="RUNTIME_SERVICES_UNAVAILABLE")
        return LocalRuntimeServices(db)
    services = installed.provider.build(db)
    if not callable(getattr(services, "session", None)):
        raise ModuleSdkError("运行数据服务契约不匹配", code="RUNTIME_SERVICES_INVALID")
    return services


def bind_authenticated_session(db):
    from staffdeck_harness.modules.registry import peek_registry
    db.info.setdefault("staffdeck_registry", peek_registry())
    services = runtime_services(db)
    attach = getattr(services, "attach", None)
    if callable(attach):
        attach(db)
    db.info["staffdeck_runtime_services"] = services


def execution_identity(request: Any, session: Any):
    """The outer trusted identity is supplied by ingress, not inferred from model arguments."""
    from staffdeck_harness.contracts.runtime_services import ExecutionIdentity
    value = getattr(request, "_trusted_execution", None)
    if value is not None:
        if not isinstance(value, ExecutionIdentity) or (value.tenant_id, value.staff_id, value.session_id) != (
                request.tenant_id, session.agent_id, session.id):
            raise ModuleSdkError("执行上下文与请求不一致", code="EXECUTION_IDENTITY_MISMATCH")
        return value
    return None
