"""Kernel (K) and trusted (T) modules that are not swappable but are still *registered*.

Registering them serves three purposes:

1. the module tree is complete — every leaf of the reference architecture is a
   real registry entry with a manifest, contract version and PEP marker;
2. ``seal()`` validates their contracts and dependencies like any other module;
3. they can be *disabled* for a deployment (e.g. no scheduler, no team
   delegation) without code changes — but never replaced by a tenant plugin.

Each provider object exposes a small, explicit surface so the host that owns
the concept can resolve it from the registry instead of importing it.
"""

from __future__ import annotations

from typing import Any, Mapping

from staffdeck_dsh.contracts.manifest import ModuleKind, SlotName
from staffdeck_dsh.modules.registry import ModuleRegistry, manifest


class PersonaModule:
    module_id = "staff.persona"

    def render(self, db: Any, tenant_id: str, agent: Any) -> str | None:
        from staffdeck_dsh.composition.staff import _persona

        return _persona(db, tenant_id, agent)


class ModelRouteModule:
    module_id = "staff.model_route"

    def route(self, db: Any, tenant_id: str, agent_id: str | None) -> Mapping[str, str]:
        from staffdeck_dsh.composition.projection import agent_model_route

        return agent_model_route(db, tenant_id, agent_id)


class CompositionProjectionModule:
    module_id = "composition.projection"

    def project(self, db: Any, tenant_id: str, agent_id: str | None):
        from staffdeck_dsh.composition.staff import project_staff

        return project_staff(db, tenant_id, agent_id)


class CompositionCompilerModule:
    module_id = "composition.compiler"

    def compile(self, staff: Any, **kw: Any):
        from staffdeck_dsh.composition.compiler import CompositionCompiler

        return CompositionCompiler().compile(staff, **kw)


class SopDefinitionModule:
    module_id = "sop.definition"

    def slots(self, content: Mapping[str, Any]):
        from staffdeck_dsh.composition.slots import sop_slots

        return sop_slots(content)


class SopSlotResolverModule:
    module_id = "sop.slots"

    def resolve(self, declarations: Any, staff_bindings: Mapping[str, str], **kw: Any):
        from staffdeck_dsh.composition.slots import resolve_slots

        return resolve_slots(declarations, staff_bindings, **kw)


class SopRuntimeModule:
    """TaskFrame/CAS state machine is legacy-owned; this entry makes it visible and disable-able."""

    module_id = "sop.runtime"

    def store(self, db: Any):
        from app.core.task_frame_store import TaskFrameStore

        return TaskFrameStore(db)


class HandoffCoreModule:
    module_id = "handoff.core"

    def build(self, db: Any, guard: Any):
        from staffdeck_dsh.handoff.core import build_handoff_core

        return build_handoff_core(db, guard)


class CancellationModule:
    module_id = "runtime.cancellation"

    def is_cancelled(self, session_id: str, turn_id: str, *, db: Any = None) -> bool:
        from app.core.cancellation import is_chat_turn_cancelled

        return is_chat_turn_cancelled(session_id, turn_id, db=db)


class TeamProviderModule:
    module_id = "team.provider"

    def planner_context(self, db: Any, team: Any):
        from app.teams.wakeup import build_team_planner_context

        return build_team_planner_context(db, team)


class WebIngressModule:
    module_id = "ingress.web"
    routes = ("/api/chat/turn", "/api/chat/stream")


class PublicApiIngressModule:
    module_id = "ingress.public_api"
    routes = ("/api/v1/*",)


class SchedulerIngressModule:
    module_id = "ingress.scheduler"

    def dispatch(self, *a: Any, **kw: Any):
        from app.scheduled_tasks import service

        return service


class ChannelHostModule:
    module_id = "channel.host"

    def build(self, db: Any, profile: Any):
        from staffdeck_dsh.channels.host import ChannelHost

        return ChannelHost(db, profile)


class RuntimeCoordinatorModule:
    module_id = "runtime.coordinator"

    def loop(self, db: Any, **kw: Any):
        from app.core.agent_loop import AgentLoop

        return AgentLoop(db, **kw)


class DshCoreModule:
    """The external DSH engine itself (Node). Version pinned by the vendored checkout."""

    module_id = "dsh.core"

    def version(self) -> str:
        import json
        import os
        from pathlib import Path

        root = os.environ.get("DSH_ROOT", "")
        try:
            return str(json.loads(Path(root, "apps", "cli", "package.json").read_text()).get("version") or "unknown")
        except Exception:
            return "unknown"


class InvocationLedgerModule:
    module_id = "ledger.invocation"

    def ledger(self, db: Any):
        from staffdeck_dsh.capabilities.ledger import InvocationLedger

        return InvocationLedger(db)


def register(registry: ModuleRegistry, ctx: Mapping[str, Any]) -> None:
    K, T = ModuleKind.KERNEL, ModuleKind.TRUSTED
    registry.install(manifest("staff.persona", "Persona 投影", kind=K, slots=[SlotName.STAFF_MODEL_ROUTE]), PersonaModule(), slot=SlotName.STAFF_MODEL_ROUTE)
    registry.install(manifest("staff.model_route", "模型路由", kind=K, slots=[SlotName.STAFF_MODEL_ROUTE], provides=["model.use/v1"], policy_actions=["model.use/v1"]), ModelRouteModule(), slot=SlotName.STAFF_MODEL_ROUTE)
    registry.mark_guarded(SlotName.STAFF_MODEL_ROUTE)
    registry.install(manifest("composition.projection", "StaffComposition 投影", kind=K, slots=[SlotName.STAFF_SOP]), CompositionProjectionModule(), slot=SlotName.STAFF_SOP)
    registry.install(manifest("composition.compiler", "组成快照编译器", kind=K, slots=[SlotName.STAFF_SOP], requires=["hook.contribute/v1"]), CompositionCompilerModule(), slot=SlotName.STAFF_SOP)
    registry.install(manifest("sop.definition", "SOP 定义与逻辑槽声明", kind=ModuleKind.CONTENT, slots=[SlotName.SOP_SLOT_CONTROL], provides=["sop.execute/v1"], policy_actions=["sop.execute/v1"]), SopDefinitionModule(), slot=SlotName.SOP_SLOT_CONTROL)
    registry.install(manifest("sop.slots", "逻辑槽解析", kind=K, slots=[SlotName.SOP_SLOT_CONTROL]), SopSlotResolverModule(), slot=SlotName.SOP_SLOT_CONTROL)
    registry.mark_guarded(SlotName.SOP_SLOT_CONTROL)
    registry.install(manifest("sop.runtime", "SOP 运行态（TaskFrame/CAS）", kind=T, slots=[SlotName.SOP_SLOT_CONTROL]), SopRuntimeModule(), slot=SlotName.SOP_SLOT_CONTROL)
    registry.install(manifest("handoff.core", "Handoff Core 状态机", kind=T, slots=[SlotName.HANDOFF_ASSIGNMENT], provides=["handoff.request/v1", "handoff.assign/v1", "handoff.reply/v1"], policy_actions=["handoff.request/v1", "handoff.assign/v1", "handoff.reply/v1"]), HandoffCoreModule(), slot=SlotName.HANDOFF_ASSIGNMENT)
    registry.install(manifest("runtime.cancellation", "取消与恢复", kind=T, slots=[SlotName.HANDOFF_ASSIGNMENT]), CancellationModule(), slot=SlotName.HANDOFF_ASSIGNMENT)
    registry.install(manifest("team.provider", "团队/子智能体委派", kind=ModuleKind.CODE, slots=[SlotName.STAFF_TEAM], provides=["team.delegate/v1"], policy_actions=["team.delegate/v1"]), TeamProviderModule(), slot=SlotName.STAFF_TEAM)
    registry.mark_guarded(SlotName.STAFF_TEAM)
    registry.install(manifest("ingress.web", "Web / API 入口", kind=ModuleKind.CODE, slots=[SlotName.STAFF_INGRESS], provides=["runtime.turn/v1"], policy_actions=["staff.use/v1"]), WebIngressModule(), slot=SlotName.STAFF_INGRESS)
    registry.install(manifest("ingress.public_api", "开放接口入口", kind=ModuleKind.CODE, slots=[SlotName.STAFF_INGRESS], provides=["runtime.turn/v1"], policy_actions=["staff.use/v1"]), PublicApiIngressModule(), slot=SlotName.STAFF_INGRESS)
    registry.install(manifest("ingress.scheduler", "定时任务触发", kind=ModuleKind.CODE, slots=[SlotName.STAFF_INGRESS], provides=["runtime.turn/v1"], policy_actions=["staff.use/v1"]), SchedulerIngressModule(), slot=SlotName.STAFF_INGRESS)
    registry.mark_guarded(SlotName.STAFF_INGRESS)
    registry.install(manifest("channel.host", "Channel Host（Inbox/Outbox + 收发 PEP）", kind=T, slots=[SlotName.STAFF_CHANNEL], provides=["channel.receive/v1", "channel.send/v1"], policy_actions=["channel.receive/v1", "channel.send/v1"]), ChannelHostModule(), slot=SlotName.STAFF_CHANNEL)
    registry.install(manifest("runtime.coordinator", "Runtime Coordinator（AgentLoop）", kind=K, slots=[SlotName.RUNTIME_KERNEL]), RuntimeCoordinatorModule(), slot=SlotName.RUNTIME_KERNEL)
    registry.install(manifest("dsh.core", "DSH Core AgentLoop", kind=K, slots=[SlotName.RUNTIME_KERNEL]), DshCoreModule(), slot=SlotName.RUNTIME_KERNEL, enabled=bool(getattr(ctx.get("settings"), "dsh_enabled", False)))
    registry.install(manifest("ledger.invocation", "Invocation Ledger", kind=T, slots=[SlotName.EVENT_OBSERVER], provides=["event.observe/v1"]), InvocationLedgerModule(), slot=SlotName.EVENT_OBSERVER)
