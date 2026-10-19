"""仅知识库数字员工的确定性预检索（conversation 直查）。

背景：仅绑定知识库的数字员工（无技能/SOP/工具），几乎每个问题都应该查
知识库，但现状要走一整轮 LLM 决策（模型决定调 knowledge_search）才开始
检索——冷态多花 3~6s，且多占一次模型交互。

这里复用 SOP 场景 C 的预填范式（``sop_step_executor.plan_sop_prefill_actions``）：
直接产出 knowledge_search 动作塞进 harness 的 ``pending_actions``，循环
第一轮就执行检索（0 次 LLM），模型第一次被调用时结果已在 transcript 里，
基于它直接作答——整条链路只保留一个回答回合。

判定策略（2026-10-19 起为纯确定性，已移除轻量 LLM 二元判定）：
- 寒暄词典快筛（0 成本）：去尾部语气词/标点后**完全匹配**才拦；混合消息
  （「你好，我想问年假」）不命中。
- 其余一律预填。理由是成本不对称已经翻转：路由小候选短路 + 路缓存落地后
  一次误查只有 ~0.5s，而判否要退回一整轮决策（实测 5.7s）；且实测二元
  判定器（``knowledge.prefetch_judge``）在同句上 true/false 摇摆、单次
  0.98~11.1s，净期望为负。
- 误查代价：纯寒暄通常 0 chunk 提前停；引用卡片按回复里实际出现的 ``[n]``
  裁剪（``compact_knowledge_citation_labels``），不会给寒暄硬挂一排引用。
  模型后续轮仍可自己调 knowledge_search 换词重搜（工具保留在 manifest 里，
  预填只是快路径不是唯一路径）。

边界（第一版窄场景）：
- 仅 conversation kind；SOP 需求由 sop_step_executor 场景 C 负责，不介入。
- 仅「新帧首轮」（transcript 为空）。同帧续轮/恢复不介入，避免重复检索。
- 仅「可用能力里没有 skill/general_skill/tool」的需求（knowledge_base /
  internal / file kind 不阻断：前者是授权描述，internal 是知识检索自身，
  file 是附件读取，与预填正交）。
- 任何异常返回空列表，静默回退现有 LLM 决策链路。
"""

from __future__ import annotations

from typing import Any

QUERY_MAX_CHARS = 200
# 与 knowledge_search 工具侧上限对齐（harness_capability_invoker min(…, 12)）
MAX_CHUNKS = 8
# 可用能力里出现这些 kind 说明不止知识库（有技能/工具要模型自己编排），不预填。
# CapabilityKind 全集 = {general_skill, knowledge, tool, file, internal}：
# knowledge 是授权描述、internal 是知识检索自身、file 是附件读取，均与预填正交。
_BLOCKING_KINDS = {"general_skill", "tool"}
TRACE_EVENT = "knowledge_prefetch_conversation"

# 纯寒暄词典快筛：命中直接跳过预填（0 成本拦最明显的）。
# 只做「去标点/语气词后完全匹配」，绝不前缀匹配——「你好，我想问年假」不是寒暄。
_SMALLTALK_EXACT = {
    "hi", "hii", "hiii", "hello", "hey", "yo", "halo",
    "你好", "您好", "在吗", "在么", "在不在", "嗨", "哈喽", "哈罗",
    "hello there", "hi there",
    # 纯礼貌性结束/确认，不含任何企业事实诉求（原由 LLM 判定器栏，现入词典）
    "谢谢", "多谢", "感谢", "辛苦了", "再见", "拜拜", "好的", "收到",
}
_SMALLTALK_TRAILING = "呀哦哈呢吧嘛啊哟嘿哇啦咯嘞呐喔嗷呜捏~～!！?？。.，, 　"


def _normalize_smalltalk(message: str) -> str:
    """去首尾空白 + 去尾部语气词/标点，小写化，供寒暄完全匹配。"""
    text = message.strip().lower()
    changed = True
    while changed and text:
        changed = False
        if text and text[-1] in _SMALLTALK_TRAILING:
            text = text[:-1]
            changed = True
    return text.strip()


def is_smalltalk(message: str) -> bool:
    return _normalize_smalltalk(message) in _SMALLTALK_EXACT


def plan_knowledge_prefill_actions(
    requirement: Any,
    *,
    first_turn: bool = True,
    trace_sink: Any = None,
) -> list[dict[str, Any]]:
    """为仅知识库的 conversation 需求产出 knowledge_search 预填动作。

    不满足窄场景条件时返回空列表，harness 侧静默回退现有链路。判定只保留
    0 成本寒暄词典：命中寒暄不预填，其余一律预填（不用 LLM 判「要不要查」）。
    """

    try:
        if not first_turn:
            return []
        if getattr(requirement, "kind", "") != "conversation":
            return []
        # SOP 节点强制 kb 是 sop_step_executor 场景 C 的领域
        if getattr(requirement, "required_knowledge_base_ids", None):
            return []
        query = str(getattr(requirement, "source_user_message", "") or "").strip()
        if len(query) < 2:
            return []  # 空消息/单字符（纯标点、表情）不值得检索
        manifest = getattr(requirement, "capability_manifest", None)
        if manifest is None or "knowledge_search" not in manifest.allowed_names():
            return []
        blocking = [
            item.name
            for item in manifest.available
            if item.available and str(item.kind or "") in _BLOCKING_KINDS
        ]
        if blocking:
            return []
        # 唯一判定：寒暄词典快筛（0 成本；只做去语气词后的完全匹配，
        # 「你好，我想问年假」这类混合消息不会命中）→ 其余一律预填。
        if is_smalltalk(query):
            return []

        action = {
            "action": "tool",
            "tool_name": "knowledge_search",
            "arguments": {
                "query": query[:QUERY_MAX_CHARS],
                "max_chunks": MAX_CHUNKS,
                # 不传 knowledge_base_ids：invoker 默认取 TaskFrame 全部授权
                # 范围（_search_knowledge selected = sorted(allowed)）
            },
            "task_summary": "知识库直查预填（跳过 LLM 决策轮）。",
        }
        if callable(trace_sink):
            try:
                trace_sink(
                    TRACE_EVENT,
                    {
                        "query": query[:QUERY_MAX_CHARS],
                        "frame_id": str(getattr(requirement, "task_frame_id", "") or ""),
                    },
                )
            except Exception:  # noqa: BLE001 - 观测不阻断主链路
                pass
        return [action]
    except Exception:  # noqa: BLE001 - 执行器绝不阻断主链路，一律降级
        return []