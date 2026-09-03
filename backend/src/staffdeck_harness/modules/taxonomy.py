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
from typing import Iterable, Literal, Mapping

from staffdeck_harness.contracts.manifest import SlotName

PlacementSource = Literal["override", "taxonomy", "manifest", "slot", "none"]
UNPLACED_BIG = "unplaced"
UNPLACED_SUB = "unplaced.all"


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
        description="定义每位数字员工：岗位与人设、使用的模型、绑定的能力、流程、渠道和团队，以及发布上线。",
        subs=(
            SubModule("staff.profile", "岗位、人设与模型", "员工是谁、怎么说话、用哪个模型。", "C", slots=(SlotName.STAFF_MODEL_ROUTE,), module_ids=("staff.persona", "staff.model_route"), legacy=("app.agents", "app.api.agents", "app.api.model_configs")),
            SubModule("staff.binding", "能力、流程、渠道与团队绑定", "员工能用什么、按什么流程做事、在哪些渠道工作、属于哪个团队；修改后下一轮对话生效。", "C", slots=(SlotName.STAFF_SOP,), module_ids=("composition.projection",), legacy=("agent_resource_bindings", "agent_model_bindings", "channel_bindings")),
            SubModule("staff.publish", "发布与版本", "发布前校验配置是否完整，生成对话时实际使用的版本，支持上下线。", "K", module_ids=("composition.compiler",), legacy=("staffdeck_harness.composition.compiler",)),
        ),
        edges=(("sop", "装配流程"), ("interaction", "装配交互能力"), ("runtime", "提交执行")),
    ),
    BigModule(
        id="sop", name="流程与 SOP", root="SOP 管理与运行", order=2,
        description="让员工按既定流程办事：定义步骤，逐步推进，执行中把关。",
        subs=(
            SubModule("sop.definition", "流程定义与版本", "步骤、走向、触发条件和每一步所需的能力；支持草稿、发布与分享。", "C", slots=(SlotName.SOP_SLOT_KNOWLEDGE, SlotName.SOP_SLOT_SKILL, SlotName.SOP_SLOT_ACTION, SlotName.SOP_SLOT_CONTROL), module_ids=("sop.definition", "sop.slots"), legacy=("app.skills", "app.api.skills")),
            SubModule("sop.runtime", "流程执行与恢复", "记录流程执行到哪一步，中断后可以恢复。", "T", module_ids=("sop.runtime",), legacy=("app.core.task_frame_store", "app.core.harness_turn_store")),
            SubModule("sop.supervision", "执行前准备与执行后把关", "回答前载入当前流程步骤；回答后检查输出是否符合流程要求。", "T", slots=(SlotName.STAFF_INTERACTION,), module_ids=("interaction.default",), legacy=("staffdeck_harness.interactions",)),
        ),
        edges=(("capability", "装配所需能力"), ("runtime", "流程上下文与监管")),
    ),
    BigModule(
        id="capability", name="业务能力中心", root="统一能力中心", order=3,
        description="员工可以使用的所有能力：查知识库、用技能、调工具、执行命令与文件操作。每次调用都会检查权限并留下记录。",
        subs=(
            SubModule("capability.knowledge", "知识库", "导入资料、建立索引、检索并标注引用来源。", "A", module_ids=("knowledge.local",), legacy=("app.knowledge",)),
            SubModule("capability.skill", "通用技能", "以技能包的形式扩展员工会做的事。", "A", module_ids=("general_skill.local",), legacy=("app.general_skills",)),
            SubModule("capability.tool", "工具调用", "调用 HTTP 接口、MCP 服务或其他智能体。", "A", module_ids=("tool.local",), legacy=("app.tools",)),
            SubModule("capability.execution", "记忆、执行环境与工作产物", "记忆读写、受控地执行命令和文件操作、输出工作产物。", "T", module_ids=("sandbox.local",), legacy=("app.harness", "app.memory")),
        ),
    ),
    BigModule(
        id="interaction", name="交互与协作", root="统一交互与协作", order=4,
        description="需要人参与的环节：通知处理人、转人工、等待回复后继续，以及团队内协作。",
        subs=(
            SubModule("interaction.notification", "通知与回复渠道", "通过站内、飞书、钉钉、企业微信、微信通知处理人并接收回复。", "A", slots=(SlotName.HANDOFF_NOTIFIER, SlotName.HANDOFF_REPLY_ENDPOINT)),
            SubModule("interaction.human_task", "转人工任务与指派", "任务从发起、指派、处理到关闭的全过程。", "T", slots=(SlotName.HANDOFF_ASSIGNMENT,), legacy=("app.core.human_handoff_service",)),
            SubModule("interaction.resume", "等待、中断与恢复", "等待人工回复期间可以中断，收到回复后自动继续。", "T", module_ids=("handoff.core", "runtime.cancellation"), legacy=("app.api.chat._apply_handoff_reply",)),
            SubModule("interaction.team", "团队协作", "把任务派给团队中的其他数字员工共同完成。", "A", slots=(SlotName.STAFF_TEAM,), module_ids=("team.provider",), legacy=("app.teams",)),
        ),
    ),
    BigModule(
        id="channel", name="渠道接入与任务触发", root="统一接入与投递", order=5,
        description="用户从哪里找到员工：飞书、钉钉、企业微信、微信、网页、开放接口，以及定时任务。",
        subs=(
            SubModule("channel.im", "飞书、钉钉、企业微信与微信", "接入各个即时通讯平台，处理消息、附件与卡片。", "A", slots=(SlotName.STAFF_CHANNEL,), legacy=("app.channels.adapters",)),
            SubModule("channel.external", "网页与开放接口", "网页对话、开放接口与命令行入口。", "A", slots=(SlotName.STAFF_INGRESS,), module_ids=("ingress.web", "ingress.public_api"), legacy=("app.api.chat", "app.public_api")),
            SubModule("channel.scheduler", "定时任务", "按设定时间自动发起任务。", "A", module_ids=("ingress.scheduler",), legacy=("app.scheduled_tasks",)),
            SubModule("channel.message", "消息收发", "统一接收消息、去重，并把回复投递到对应渠道。", "T", module_ids=("channel.host",), legacy=("app.channels.service_durable_inbox", "app.channels.service_outbox")),
        ),
        edges=(("staff", "选择数字员工"),),
    ),
    BigModule(
        id="runtime", name="对话与执行引擎", root="统一运行协调", order=6,
        description="员工回答问题时背后的执行引擎：接收请求、规划步骤、调用能力、生成回复。",
        subs=(
            SubModule("runtime.coordinator", "对话调度", "管理会话与每轮对话，协调流程推进与回复生成。", "K", slots=(SlotName.RUNTIME_KERNEL,), module_ids=("runtime.coordinator",), legacy=("app.core.agent_loop", "app.core.harness_v2_engine")),
            SubModule("runtime.bridge", "执行引擎", "可选 Harness v3 或 Harness v2 引擎，同一时间只启用一个。", "T", slots=(SlotName.RUNTIME_ENGINE,), legacy=("staffdeck_harness.bridge",)),
            SubModule("runtime.agentloop", "引擎核心", "Harness v3 引擎本体，以独立进程运行。", "K", module_ids=("harness_v3.core",)),
        ),
        edges=(("capability", "调用能力"), ("interaction", "通知、转发或等待回复"), ("channel", "输出消息"), ("governance", "投影运行事件")),
    ),
    BigModule(
        id="governance", name="运行治理", root="运行治理", order=7,
        description="记录运行过程，收集反馈，处理异常。",
        subs=(
            SubModule("governance.trace", "执行轨迹", "把每轮对话的执行过程记录下来，随时可以回看。", "T", module_ids=("observer.trace",), legacy=("app.observability", "app.api.traces")),
            SubModule("governance.feedback", "反馈与改进", "收集用户反馈，供分析与能力改进。", "A", module_ids=("observer.feedback",), legacy=("app.feedback", "app.api.evolution")),
            SubModule("governance.monitoring", "调用记录与异常处理", "记录每次能力调用；结果不明时可以人工确认。", "T", module_ids=("ledger.invocation",), legacy=("harness_invocations", "app.core.harness_recovery")),
        ),
    ),
    BigModule(
        id="permission", name="统一权限", root="统一 PEP 接口", order=8, pep=False,
        description="决定谁能使用什么，贯穿所有模块。按部署版本二选一，任何模块都不能绕过。",
        subs=(
            SubModule("permission.oss_local", "开源版 · 本地权限", "按租户、角色、员工归属和绑定关系判断。", "K", module_ids=("security.oss_local",), legacy=("app.security.permissions",)),
            SubModule("permission.business_base", "企业版 · 统一权限中心", "由企业权限中心判定；权限中心不可用时拒绝访问，不会放行。", "K", module_ids=("security.business_base",)),
        ),
    ),
)


def big_module_ids() -> list[str]:
    return [b.id for b in sorted(TAXONOMY, key=lambda b: b.order)]


SUB_INDEX: dict[str, tuple[BigModule, SubModule]] = {sub.id: (big, sub) for big in TAXONOMY for sub in big.subs}


def sub_ids() -> list[str]:
    return [sub.id for big in sorted(TAXONOMY, key=lambda b: b.order) for sub in big.subs]


def resolve_placement(module_id: str, slot: SlotName | None, *, category: str = "", override: str | None = None) -> tuple[BigModule, SubModule, PlacementSource] | None:
    """Where a module is displayed: operator override > curated taxonomy > manifest category > slot default."""

    if override and override in SUB_INDEX:
        big, sub = SUB_INDEX[override]
        return big, sub, "override"
    for big in TAXONOMY:
        for sub in big.subs:
            if module_id in sub.module_ids:
                return big, sub, "taxonomy"
    if category and category in SUB_INDEX:
        big, sub = SUB_INDEX[category]
        return big, sub, "manifest"
    if slot is not None:
        for big in TAXONOMY:
            for sub in big.subs:
                if slot in sub.slots:
                    return big, sub, "slot"
    return None


def sub_for(module_id: str, slot: SlotName) -> tuple[BigModule, SubModule] | None:
    hit = resolve_placement(module_id, slot)
    return (hit[0], hit[1]) if hit else None


FIXED_SLOTS = {SlotName.RUNTIME_ENGINE.value, SlotName.SECURITY_PEP.value}


def movable(module: Mapping) -> bool:
    """Engines / PEP providers and kernel entries stay where the taxonomy puts them."""

    return module.get("kind") != "K" and module.get("slot") not in FIXED_SLOTS


def tree(registry_modules: Iterable[dict], placements: Mapping[str, str] | None = None) -> list[dict]:
    """Merge the flat registry listing into the taxonomy tree for the API.

    ``placements`` are operator overrides (module_id → sub id) from the saved
    assembly; they only affect display, never resolution, so they need no
    restart.
    """

    placements = dict(placements or {})
    by_key: dict[tuple[str, str], list[dict]] = {}
    unplaced: list[dict] = []
    for raw in registry_modules:
        m = dict(raw)
        try:
            slot: SlotName | None = SlotName(m["slot"])
        except ValueError:
            slot = None
        m["movable"] = movable(m)
        override = placements.get(m["module_id"]) if m["movable"] else None
        hit = resolve_placement(m["module_id"], slot, category=str(m.get("category") or ""), override=override)
        if hit is None:
            m["placement"] = {"big_id": UNPLACED_BIG, "sub_id": UNPLACED_SUB, "source": "none"}
            unplaced.append(m)
            continue
        big, sub, source = hit
        m["placement"] = {"big_id": big.id, "sub_id": sub.id, "source": source}
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
        out.append({
            "id": UNPLACED_BIG, "name": "未归类模块", "root": "未归类", "order": 99, "pep": False, "edges": [], "hint": "placeable",
            "description": "已安装但尚未归入任何类目的模块。在每一行选择所属类目后立即生效，不需要重启。",
            "subs": [{"id": UNPLACED_SUB, "name": "未归类", "description": "", "kind": "A", "slots": [], "legacy": [], "modules": unplaced, "enabled": sum(1 for x in unplaced if x.get("enabled")), "total": len(unplaced)}],
            "enabled": sum(1 for x in unplaced if x.get("enabled")), "total": len(unplaced),
        })
    return out


def tree_options() -> list[dict]:
    """Flat list of selectable sub-modules for the placement picker."""

    return [{"sub_id": sub.id, "big_id": big.id, "label": f"{big.name} › {sub.name}"} for big in sorted(TAXONOMY, key=lambda b: b.order) for sub in big.subs]
