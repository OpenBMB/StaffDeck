"""SOP 确定性步骤执行器（P1 / P3 / P4 / P5）。

Harness 循环里每个节点推进都要一整轮 task_action LLM 决策（25-187s/轮），
但其中大部分场景的决策是纯代码可判定的——SOP 图结构（skill_schema.SkillCard）
在 TaskRequirement 编译期已包含全部判定所需信息：

- **C 预检索**：节点声明了强制知识库且槽位齐 → 直接产出 knowledge_search
  动作，跳过「LLM 决策调检索」那一轮。
- **B 纯流转直通**：节点无槽位缺口、无强制能力、且有唯一无条件出边 →
  直接 finish(completed, next_step_id)，外层 continue_frame 循环会立即
  编译下一节点继续，整个节点零 LLM。
- **A 缺槽直通**：节点缺槽位且无强制能力 → 直接 finish(awaiting_user)，
  问话用模板文案（LLM 原本那轮只为把这句话说得更自然，P2 再用轻量模型润色）。
- **D 决策直判**（P3）：`decision` 节点的分支条件能用槽位取值直接证定时，
  用**条件中文词元的双向包含**做保守匹配，命中唯一才直判。
- **E 工具直组**（P3）：`tool_call` 节点的 input_schema 必填参数全有同名槽位
  → 直接产出 tool 动作，省掉 capability_describe + LLM 组参两轮。
- **F 转人工直通**（P4）：handoff 终点且无出边 → 直接 finish(handoff)。
- **G 条件求值直判**（P5，本文件新增）：边条件若已被**离线编译器**
  （``app/skills/edge_condition_compiler.py``）编译成结构化契约
  （``EdgeConditionSpec``），直接用槽位取值 + 上一步调用结果 + 用户消息
  求值——命中唯一即直判，**零 LLM**。这是把「每次推进都让模型重新理解一遍
  中文条件」换成「编译一次、跑无数次」的关键一步。

设计约束：
- 场景 G 只在**恰好一条**边命中、且没有「未知」的互斥边时才直判；只要存在
  一条无法求值的边（含 ``llm_judge``、未编译的自由文本、字段无从解析），
  一律交还 LLM——**未知不等于不命中**。
- 场景 D 是场景 G 的**兜底**：只在「这些边一条都没编译过」且节点类型是
  ``decision`` 时启用，保证未编译的历史技能行为完全不变。
- 断点恢复到同一步骤（same_step）默认不介入；仅恢复补槽
  （``resumed_awaiting_user``）时放行 A 与 F/D/G/B（C 预检索仍排除，
  避免重复检索）——恢复帧槽位已齐时旧实现会静默放弃、整帧白落 LLM。
- 任何异常返回空列表，harness 侧静默回退现有 LLM 决策链路。
- 返回动作 dict（HarnessAction 兼容），由 harness_agent 侧
  model_validate——独立模块避免循环 import。
- 模块布局（2026-09-15 拆分）：本文件只保留入口与调度（plan_sop_prefill_actions）；场景上下文/转移表/动作构造在 ``app/core/sop_scenes.py``；出边求值纯函数在 ``app/core/sop_edge_eval.py``。依赖方向 executor → scenes → edge_eval。
"""


from __future__ import annotations

from typing import Any, Mapping

from app.core.base.graph_rules import GraphRules
from app.core.sop.scenes import SCENES, SceneContext, TRACE_EVENT  # noqa: F401  (TRACE_EVENT 供测试/诊断 re-export)

__all__ = [
    "SCENES",
    "SceneContext",
    "TRACE_EVENT",
    "plan_sop_prefill_actions",
    "slot_satisfied",
]


def plan_sop_prefill_actions(
    requirement: Any,
    *,
    same_step: bool = False,
    resumed_awaiting_user: bool = False,
    satisfied_required_knowledge_ids: set[str] | None = None,
    trace_sink: Any = None,
    slot_extraction_model: Any = None,
    edge_condition_specs: Mapping[str, Any] | None = None,
    slot_submission: Mapping[str, Any] | None = None,
    laya_router: Any = None,
) -> list[dict[str, Any]]:
    """为 sop TaskRequirement 产出确定性动作序列；不可判定时返回空列表。

    场景调度是**有序转移表**（``SCENES``）：表序即优先级，每个表项为
    ``(guard, run)``——guard 命中且 run 返回动作即终局；run 返回 None 表示
    本场景不可判定，继续匹配下一表项。新增场景 = 追加一行表项，场景间的
    优先/互斥关系由表结构表达，不再依赖 if-elif 阶梯里的人工推演。

    slot_submission：用户在**表单**（A2UI）里提交的结构化取值。带值时场景 A
    直接用它当抽取结果，**跳过那一轮槽位抽取 LLM**；字段白名单与 LLM 抽取
    一致（``expected_user_info or required_slots``），不在白名单里的键丢弃。

    resumed_awaiting_user：断点恢复到同一步骤且上次终点是本执行器发出的
    awaiting_user（等槽位）。此时用户回来补信息正是槽位抽取的主场景：
    放行场景 A；槽位已齐（无缺口）时同样放行 F/D/G/B 直通——2026-09-15
    修复的死胡同：旧实现里恢复帧槽位已齐会静默 return []，整帧白白落
    一轮 LLM。场景 C（预检索）在恢复帧仍不介入，避免重复检索。

    edge_condition_specs：``{条件指纹: EdgeConditionSpec 载荷}``，由 harness
    侧按技能加载（``app/skills/edge_condition_jobs`` 的编译产物）。**不放进
    TaskRequirement**——那份对象每轮都会整包发给模型，塞进去会白白撑大
    prompt。缺省/为空时全部退回场景 D 与 LLM 决策，行为与改造前一致。

    laya_router：可选的 Laya 出边决策旁路（``app/core/laya_router.py``）。
    为 None / 未启用时场景 L 的 guard 不通过，行为与改造前完全一致。
    """

    try:
        if requirement is None or getattr(requirement, "kind", "") != "sop":
            return []
        resume_slot_recovery = bool(same_step and resumed_awaiting_user)
        if same_step and not resume_slot_recovery:
            # 断点恢复到同一步骤：transcript 已有中间状态，预填可能重复动作
            return []

        sop_context = getattr(requirement, "sop_context", None)
        step = sop_context.get("step") if isinstance(sop_context, dict) else None
        if not isinstance(step, dict) or not step:
            return []

        ctx = SceneContext(
            requirement=requirement,
            step=step,
            sop_context=sop_context if isinstance(sop_context, dict) else {},
            resume_slot_recovery=resume_slot_recovery,
            satisfied_required_knowledge_ids=satisfied_required_knowledge_ids,
            trace_sink=trace_sink,
            slot_extraction_model=slot_extraction_model,
            edge_condition_specs=edge_condition_specs,
            slot_submission=slot_submission,
            laya_router=laya_router,
        )
        for scene in SCENES:
            if not scene.guard(ctx):
                continue
            result = scene.run(ctx)
            if result is not None:
                return result
        return []
    except Exception:  # noqa: BLE001 - 执行器绝不阻断主链路，一律降级
        return []



# GraphRules.slot_satisfied 供 harness 侧/测试复用（保持单一实现来源）
slot_satisfied = GraphRules.slot_satisfied
