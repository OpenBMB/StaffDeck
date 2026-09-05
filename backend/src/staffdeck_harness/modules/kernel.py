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

from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.modules.registry import ModuleRegistry, manifest
from staffdeck_harness.sop.module import SopRuntimeModule  # noqa: F401 compatibility export


class PersonaModule:
    module_id = "staff.persona"

    def render(self, db: Any, tenant_id: str, agent: Any) -> str | None:
        from staffdeck_harness.composition.staff import _persona

        return _persona(db, tenant_id, agent)


class ModelRouteModule:
    module_id = "staff.model_route"

    def route(self, db: Any, tenant_id: str, agent_id: str | None) -> Mapping[str, str]:
        from staffdeck_harness.composition.projection import agent_model_route

        return agent_model_route(db, tenant_id, agent_id)


class CompositionProjectionModule:
    module_id = "composition.projection"

    def project(self, db: Any, tenant_id: str, agent_id: str | None):
        from staffdeck_harness.composition.staff import project_staff

        return project_staff(db, tenant_id, agent_id)


class CompositionCompilerModule:
    module_id = "composition.compiler"

    def compile(self, staff: Any, **kw: Any):
        from staffdeck_harness.composition.compiler import CompositionCompiler

        return CompositionCompiler().compile(staff, **kw)


class SopDefinitionModule:
    module_id = "sop.definition"

    def slots(self, content: Mapping[str, Any]):
        from staffdeck_harness.composition.slots import sop_slots

        return sop_slots(content)


class SopSlotResolverModule:
    module_id = "sop.slots"

    def resolve(self, declarations: Any, staff_bindings: Mapping[str, str], **kw: Any):
        from staffdeck_harness.composition.slots import resolve_slots

        return resolve_slots(declarations, staff_bindings, **kw)


class HandoffCoreModule:
    module_id = "handoff.core"

    def build(self, db: Any, guard: Any):
        from staffdeck_harness.handoff.core import build_handoff_core

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

    def publish(self, db: Any, **kwargs: Any):
        from app.teams.wakeup import publish_team_planner_frames

        return publish_team_planner_frames(db, **kwargs)


class WebIngressModule:
    module_id = "ingress.web"
    routes = ("/api/chat/turn", "/api/chat/stream")

    def accept(self, request: Any):
        return request


class PublicApiIngressModule(WebIngressModule):
    module_id = "ingress.public_api"
    routes = ("/api/v1/*",)


class SchedulerIngressModule(WebIngressModule):
    module_id = "ingress.scheduler"

    def dispatch(self, *a: Any, **kw: Any):
        from app.scheduled_tasks.service import execute_scheduled_task

        return execute_scheduled_task(*a, **kw)


class ChannelHostModule:
    module_id = "channel.host"

    def build(self, db: Any, profile: Any):
        from staffdeck_harness.channels.host import ChannelHost

        return ChannelHost(db, profile)


class RuntimeCoordinatorModule:
    module_id = "runtime.coordinator"

    def loop(self, db: Any, **kw: Any):
        from app.core.agent_loop import AgentLoop

        return AgentLoop(db, **kw)


class HarnessV3CoreModule:
    """The external Harness v3 engine itself (Node). Version pinned by the vendored checkout."""

    module_id = "harness_v3.core"

    def __init__(self, root: str = "") -> None:
        self.root = root

    def version(self) -> str:
        import json
        import os
        from pathlib import Path

        root = self.root or os.environ.get("HARNESS_V3_ROOT", "")
        try:
            return str(json.loads(Path(root, "apps", "cli", "package.json").read_text()).get("version") or "unknown")
        except Exception:
            return "unknown"


class InvocationLedgerModule:
    module_id = "ledger.invocation"
    name = "ledger.invocation"

    def ledger(self, db: Any):
        from staffdeck_harness.capabilities.ledger import InvocationLedger

        return InvocationLedger(db)

    def on_event(self, tenant_id: str, session_id: str, event_type: str, payload: Any) -> None:
        # The ledger is written by the capability host itself; as an observer it only listens.
        return None


def register(registry: ModuleRegistry, ctx: Mapping[str, Any]) -> None:
    K, T = ModuleKind.KERNEL, ModuleKind.TRUSTED
    registry.install(manifest("staff.persona", "员工人设", summary="把岗位、性格和说话方式整理成员工的自我设定。", kind=K, slots=[SlotName.STAFF_MODEL_ROUTE]), PersonaModule(), slot=SlotName.STAFF_MODEL_ROUTE)
    registry.install(manifest("staff.model_route", "模型分配", summary="决定员工在默认、路由和分步场景下分别使用哪个模型。", kind=K, slots=[SlotName.STAFF_MODEL_ROUTE], provides=["model.use/v1"], policy_actions=["model.use/v1"]), ModelRouteModule(), slot=SlotName.STAFF_MODEL_ROUTE)
    registry.mark_guarded(SlotName.STAFF_MODEL_ROUTE)
    registry.install(manifest("composition.projection", "员工配置汇总", summary="把员工的人设、模型、能力、流程、渠道和团队汇总成一份完整配置。", kind=K, slots=[SlotName.STAFF_SOP]), CompositionProjectionModule(), slot=SlotName.STAFF_SOP)
    registry.install(manifest("composition.compiler", "配置校验与发布", summary="发布前检查配置是否完整、依赖是否成环，并生成对话时实际使用的版本。", kind=K, slots=[SlotName.STAFF_SOP]), CompositionCompilerModule(), slot=SlotName.STAFF_SOP)
    registry.install(manifest("sop.definition", "流程定义", summary="定义流程的步骤、走向，以及每一步需要的能力。", kind=ModuleKind.CONTENT, slots=[SlotName.SOP_SLOT_CONTROL], provides=["sop.execute/v1"], policy_actions=["sop.execute/v1"]), SopDefinitionModule(), slot=SlotName.SOP_SLOT_CONTROL)
    registry.install(manifest("sop.slots", "流程能力关联", summary="把流程中声明的能力需求与员工实际绑定的资源对应起来。", kind=K, slots=[SlotName.SOP_SLOT_CONTROL]), SopSlotResolverModule(), slot=SlotName.SOP_SLOT_CONTROL)
    registry.mark_guarded(SlotName.SOP_SLOT_CONTROL)
    registry.install(manifest("sop.runtime", "流程执行推进", summary="独立管理流程实例、节点推进、挂起恢复与完成判定。", kind=T, slots=[SlotName.RUNTIME_SOP], provides=["sop.lifecycle/v1"], policy_actions=["sop.execute/v1"], metadata={"switchable": True}), SopRuntimeModule(), slot=SlotName.RUNTIME_SOP)
    registry.mark_guarded(SlotName.RUNTIME_SOP)
    registry.install(manifest("handoff.core", "转人工处理流程", summary="管理转人工任务从发起、指派、回复到关闭的全过程。", kind=T, slots=[SlotName.HANDOFF_ASSIGNMENT], provides=["handoff.request/v1", "handoff.assign/v1", "handoff.reply/v1"], policy_actions=["handoff.request/v1", "handoff.assign/v1", "handoff.reply/v1"]), HandoffCoreModule(), slot=SlotName.HANDOFF_ASSIGNMENT)
    registry.install(manifest("runtime.cancellation", "中断与恢复", summary="支持随时停止对话，并在人工回复后继续。", kind=T, slots=[SlotName.HANDOFF_ASSIGNMENT]), CancellationModule(), slot=SlotName.HANDOFF_ASSIGNMENT)
    registry.install(manifest("team.provider", "团队协作", summary="把任务派发给团队中的其他数字员工共同完成。", kind=ModuleKind.CODE, slots=[SlotName.STAFF_TEAM], provides=["team.delegate/v1"], policy_actions=["team.delegate/v1"]), TeamProviderModule(), slot=SlotName.STAFF_TEAM)
    registry.mark_guarded(SlotName.STAFF_TEAM)
    registry.install(manifest("ingress.web", "网页与接口入口", summary="接收来自网页对话和接口的请求。", kind=ModuleKind.CODE, slots=[SlotName.STAFF_INGRESS], provides=["runtime.turn/v1"], policy_actions=["staff.use/v1"]), WebIngressModule(), slot=SlotName.STAFF_INGRESS)
    registry.install(manifest("ingress.public_api", "开放接口入口", summary="接收第三方系统通过开放接口发来的请求。", kind=ModuleKind.CODE, slots=[SlotName.STAFF_INGRESS], provides=["runtime.turn/v1"], policy_actions=["staff.use/v1"]), PublicApiIngressModule(), slot=SlotName.STAFF_INGRESS)
    registry.install(manifest("ingress.scheduler", "定时任务触发", summary="按设定的时间自动发起对话任务。", kind=ModuleKind.CODE, slots=[SlotName.STAFF_INGRESS], provides=["runtime.turn/v1"], policy_actions=["staff.use/v1"]), SchedulerIngressModule(), slot=SlotName.STAFF_INGRESS)
    registry.mark_guarded(SlotName.STAFF_INGRESS)
    registry.install(manifest("channel.host", "消息收发中心", summary="统一接收各渠道消息并投递回复，收发都会检查权限。", kind=T, slots=[SlotName.STAFF_CHANNEL], provides=["channel.receive/v1", "channel.send/v1"], policy_actions=["channel.receive/v1", "channel.send/v1"]), ChannelHostModule(), slot=SlotName.STAFF_CHANNEL)
    registry.install(manifest("runtime.coordinator", "对话调度器", summary="协调每轮对话：接收请求、规划任务、推进流程并生成回复。", kind=K, slots=[SlotName.RUNTIME_KERNEL]), RuntimeCoordinatorModule(), slot=SlotName.RUNTIME_KERNEL)
    registry.install(manifest("harness_v3.core", "Harness v3 引擎核心", summary="Harness v3 引擎本体，以独立进程运行，负责多步推理与工具调用。", kind=K, slots=[SlotName.RUNTIME_KERNEL]), HarnessV3CoreModule(str(getattr(ctx.get("settings"), "harness_v3_root", "") or "")), slot=SlotName.RUNTIME_KERNEL, enabled=bool(getattr(ctx.get("settings"), "harness_v3_enabled", False)))
    registry.install(manifest("ledger.invocation", "调用记录", summary="记录每一次能力调用及其结果，避免重复执行有副作用的操作。", kind=T, slots=[SlotName.EVENT_OBSERVER], provides=["event.observe/v1"]), InvocationLedgerModule(), slot=SlotName.EVENT_OBSERVER)
