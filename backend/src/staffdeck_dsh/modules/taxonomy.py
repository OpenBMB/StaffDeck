"""Big-module / small-module taxonomy.

The runtime registry is flat (one manifest per pluggable thing). The product
view groups those into the eight business modules of the reference
architecture, each with its sub-modules. This mapping is *data*, kept in one
place, so the admin API and the frontend tree render the same hierarchy, and
a deployment can re-parent a plugin without touching code paths that run.

    Big module ─── Sub-module ─── registry slot(s) / module ids

Kind letters follow the design: A code plugin, C content package, T trusted
service, K kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from staffdeck_dsh.contracts.manifest import SlotName


@dataclass(frozen=True)
class SubModule:
    id: str
    name: str
    description: str
    kind: str                                      # A | C | T | K (dominant kind)
    slots: tuple[SlotName, ...] = ()               # registry slots that belong here
    module_ids: tuple[str, ...] = ()               # explicit registry modules that belong here
    legacy: tuple[str, ...] = ()                   # legacy backend areas this wraps (informational)


@dataclass(frozen=True)
class BigModule:
    id: str
    name: str
    description: str
    root: str                                      # the "root" node label from the diagram
    order: int
    subs: tuple[SubModule, ...] = ()
    pep: bool = True                               # guarded by the unified PEP
    edges: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (target big-module id, label)


TAXONOMY: tuple[BigModule, ...] = (
    BigModule(
        id="staff", name="数字员工管理与装配", root="数字员工定义与装配", order=1,
        description="一个数字员工的顶层装配根：岗位、模型路由、绑定与发布。",
        subs=(
            SubModule("staff.profile", "岗位、角色与模型配置", "Persona、模型路由（default/router/step）、会话策略。", "C", slots=(SlotName.STAFF_MODEL_ROUTE,), module_ids=("staff.persona", "staff.model_route"), legacy=("app.agents", "app.api.agents", "app.api.model_configs")),
            SubModule("staff.binding", "SOP、能力、渠道和团队绑定", "把 SOP 逻辑槽、能力、渠道与团队绑定到员工；下一 Turn 生效。", "C", slots=(SlotName.STAFF_SOP,), module_ids=("composition.projection",), legacy=("agent_resource_bindings", "agent_model_bindings", "channel_bindings")),
            SubModule("staff.publish", "发布、版本与上下线", "组成快照编译与校验（Required Slot、契约、依赖环、Hook 环），上架/下架。", "K", module_ids=("composition.compiler",), legacy=("staffdeck_dsh.composition.compiler",)),
        ),
        edges=(("sop", "装配流程"), ("interaction", "装配交互能力"), ("runtime", "提交执行")),
    ),
    BigModule(
        id="sop", name="流程与 SOP", root="SOP 管理与运行", order=2,
        description="SOP 定义只声明逻辑槽；运行态状态机唯一，不可被租户替换。",
        subs=(
            SubModule("sop.definition", "流程定义、生成与版本", "节点、边、触发规则、能力槽、输出规则；草稿/发布/版本/分享。", "C", slots=(SlotName.SOP_SLOT_KNOWLEDGE, SlotName.SOP_SLOT_SKILL, SlotName.SOP_SLOT_ACTION, SlotName.SOP_SLOT_CONTROL), module_ids=("sop.definition", "sop.slots"), legacy=("app.skills", "app.api.skills")),
            SubModule("sop.runtime", "流程状态、节点推进与恢复", "SopExecution / TaskFrame / 节点状态 / CAS / 恢复。", "T", module_ids=("sop.runtime",), legacy=("app.core.task_frame_store", "app.core.harness_turn_store")),
            SubModule("sop.supervision", "AgentLoop 前置装配与后置监管", "把 SOP 投影成 ExecutionSlice；pre_step / turn_stopping 监管与输出校验。", "T", slots=(SlotName.STAFF_INTERACTION,), module_ids=("interaction.default",), legacy=("staffdeck_dsh.interactions",)),
        ),
        edges=(("capability", "装配所需能力"), ("runtime", "流程上下文与监管")),
    ),
    BigModule(
        id="capability", name="业务能力中心", root="统一能力中心", order=3,
        description="所有能力经 CapabilityHost：激活围栏 → Ledger → Guarded Facade（PEP）→ 现有服务。",
        subs=(
            SubModule("capability.knowledge", "知识库：导入、索引、检索与引用", "KnowledgeService 检索 + 引用回流；Provider 可替换。", "A", module_ids=("knowledge.local",), legacy=("app.knowledge",)),
            SubModule("capability.skill", "通用技能：技能包、目录与执行", "SKILL.md 包读取到上下文；执行走沙箱工具。", "A", module_ids=("general_skill.local",), legacy=("app.general_skills",)),
            SubModule("capability.tool", "工具调用：HTTP、MCP 与智能体协作", "HTTP / MCP / A2A 统一经 ToolExecutor；副作用键幂等。", "A", module_ids=("tool.local",), legacy=("app.tools",)),
            SubModule("capability.execution", "执行支持：记忆、沙箱与工作产物", "记忆读写、受控命令与文件、产物发布。", "T", module_ids=("sandbox.local",), legacy=("app.harness", "app.memory")),
        ),
    ),
    BigModule(
        id="interaction", name="交互与协作", root="统一交互与协作", order=4,
        description="Handoff Core 状态机不可插拔；指派策略、通知器、回复解析可插拔。",
        subs=(
            SubModule("interaction.notification", "通知、转发与回复", "Web 收件箱 / 飞书 / 钉钉 / 企微 / 微信通知与回复关联。", "A", slots=(SlotName.HANDOFF_NOTIFIER, SlotName.HANDOFF_REPLY_ENDPOINT)),
            SubModule("interaction.human_task", "人工任务、指派与处理", "pending → assigned → answered → resumed → closed；指派策略候选，Core 校验。", "T", slots=(SlotName.HANDOFF_ASSIGNMENT,), legacy=("app.core.human_handoff_service",)),
            SubModule("interaction.resume", "等待、中断与恢复", "人工回复形成新 Turn；取消与恢复走原回执路径。", "T", module_ids=("handoff.core", "runtime.cancellation"), legacy=("app.api.chat._apply_handoff_reply",)),
            SubModule("interaction.team", "团队、任务与子智能体协作", "TL 派发、成员竞标、黑板与唤醒。", "A", slots=(SlotName.STAFF_TEAM,), module_ids=("team.provider",), legacy=("app.teams",)),
        ),
    ),
    BigModule(
        id="channel", name="渠道接入与任务触发", root="统一接入与投递", order=5,
        description="Durable Inbox/Outbox 包住 AgentLoop 输入输出；收/发各过一次 PEP。",
        subs=(
            SubModule("channel.im", "飞书、钉钉、企微与微信", "五个渠道适配器：验签、归一化、附件、卡片、发送。", "A", slots=(SlotName.STAFF_CHANNEL,), legacy=("app.channels.adapters",)),
            SubModule("channel.external", "网页、开放接口与命令行", "Web / Public API / CLI 入口，统一成 TurnCommand。", "A", slots=(SlotName.STAFF_INGRESS,), module_ids=("ingress.web", "ingress.public_api"), legacy=("app.api.chat", "app.public_api")),
            SubModule("channel.scheduler", "定时任务与事件触发", "定时生成 TurnCommand；冻结 SOP 快照。", "A", module_ids=("ingress.scheduler",), legacy=("app.scheduled_tasks",)),
            SubModule("channel.message", "消息标准化、渲染与投递", "Inbox 幂等、Outbox 排空、富文本渲染。", "T", module_ids=("channel.host",), legacy=("app.channels.service_durable_inbox", "app.channels.service_outbox")),
        ),
        edges=(("staff", "选择数字员工"),),
    ),
    BigModule(
        id="runtime", name="对话与执行引擎", root="统一运行协调", order=6,
        description="AgentLoop 在 DSH 之外协调会话/Turn/TaskFrame；Bridge 与 DSH 只按 Worker 代际升级。",
        subs=(
            SubModule("runtime.coordinator", "会话、Turn 与任务编排", "claim、planner、TaskFrame、租约、SOP CAS、response。", "K", slots=(SlotName.RUNTIME_KERNEL,), module_ids=("runtime.coordinator",), legacy=("app.core.agent_loop", "app.core.harness_v2_engine")),
            SubModule("runtime.bridge", "StaffDeck 与 DSH 桥接层", "EngineHost、DSH worker、能力回调 MCP、事件中继。", "T", slots=(SlotName.RUNTIME_ENGINE,), legacy=("staffdeck_dsh.bridge",)),
            SubModule("runtime.agentloop", "DSH 核心 AgentLoop", "DSH 原生 Session / Turn / Step / ToolCall（Node 子进程）。", "K", module_ids=("dsh.core",)),
        ),
        edges=(("capability", "调用能力"), ("interaction", "通知、转发或等待回复"), ("channel", "输出消息"), ("governance", "投影运行事件")),
    ),
    BigModule(
        id="governance", name="运行治理", root="运行治理", order=7,
        description="只消费事件，不改活跃快照。",
        subs=(
            SubModule("governance.trace", "事件、轨迹与运行记录", "DSH SessionEvent → RuntimeEvent → Trace/SSE/审计。", "T", module_ids=("observer.trace",), legacy=("app.observability", "app.api.traces")),
            SubModule("governance.feedback", "反馈分析与能力进化", "反馈事件消费；进化提案不修改活跃快照。", "A", module_ids=("observer.feedback",), legacy=("app.feedback", "app.api.evolution")),
            SubModule("governance.monitoring", "监控、审计与故障恢复", "Invocation Ledger、outcome_unknown 结算、恢复扫描。", "T", module_ids=("ledger.invocation",), legacy=("harness_invocations", "app.core.harness_recovery")),
        ),
    ),
    BigModule(
        id="permission", name="统一权限", root="统一 PEP 接口", order=8, pep=False,
        description="贯穿全部业务模块；部署级二选一，模块作者不可选择或卸载。",
        subs=(
            SubModule("permission.oss_local", "开源版本地权限", "tenant / role / owner / binding 规则，非 no-op。", "K", module_ids=("security.oss_local",), legacy=("app.security.permissions",)),
            SubModule("permission.business_base", "企业版 Base 权限", "Base Authz 决策 fail-closed，不回退本地。", "K", module_ids=("security.business_base",)),
        ),
    ),
)


def big_module_ids() -> list[str]:
    return [b.id for b in sorted(TAXONOMY, key=lambda b: b.order)]


def sub_for(module_id: str, slot: SlotName) -> tuple[BigModule, SubModule] | None:
    """Resolve where a registry module is displayed (explicit id wins over slot)."""

    for big in TAXONOMY:
        for sub in big.subs:
            if module_id in sub.module_ids:
                return big, sub
    for big in TAXONOMY:
        for sub in big.subs:
            if slot in sub.slots:
                return big, sub
    return None


def tree(registry_modules: Iterable[dict]) -> list[dict]:
    """Merge the flat registry listing into the taxonomy tree for the API."""

    by_key: dict[tuple[str, str], list[dict]] = {}
    unplaced: list[dict] = []
    for m in registry_modules:
        try:
            slot = SlotName(m["slot"])
        except ValueError:
            unplaced.append(m)
            continue
        hit = sub_for(m["module_id"], slot)
        if hit is None:
            unplaced.append(m)
            continue
        big, sub = hit
        by_key.setdefault((big.id, sub.id), []).append(m)
    out: list[dict] = []
    for big in sorted(TAXONOMY, key=lambda b: b.order):
        subs = []
        for sub in big.subs:
            mods = by_key.get((big.id, sub.id), [])
            subs.append({
                "id": sub.id, "name": sub.name, "description": sub.description, "kind": sub.kind,
                "slots": [s.value for s in sub.slots], "legacy": list(sub.legacy),
                "modules": mods, "enabled": sum(1 for x in mods if x.get("enabled")), "total": len(mods),
            })
        out.append({
            "id": big.id, "name": big.name, "root": big.root, "description": big.description, "order": big.order, "pep": big.pep,
            "edges": [{"to": target, "label": label} for target, label in big.edges],
            "subs": subs,
            "enabled": sum(s["enabled"] for s in subs), "total": sum(s["total"] for s in subs),
        })
    if unplaced:
        out.append({"id": "unplaced", "name": "未归类模块", "root": "未归类", "description": "已注册但未映射到任何子模块的插件。", "order": 99, "pep": False, "edges": [], "subs": [{"id": "unplaced.all", "name": "未归类", "description": "", "kind": "A", "slots": [], "legacy": [], "modules": unplaced, "enabled": sum(1 for x in unplaced if x.get("enabled")), "total": len(unplaced)}], "enabled": sum(1 for x in unplaced if x.get("enabled")), "total": len(unplaced)})
    return out
