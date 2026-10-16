"""仅知识库数字员工的确定性预检索（conversation 直查）。

背景：仅绑定知识库的数字员工（无技能/SOP/工具），几乎每个问题都应该查
知识库，但现状要走一整轮 LLM 决策（模型决定调 knowledge_search）才开始
检索——冷态多花 3~6s，且多占一次模型交互。

这里复用 SOP 场景 C 的预填范式（``sop_step_executor.plan_sop_prefill_actions``）：
直接产出 knowledge_search 动作塞进 harness 的 ``pending_actions``，循环
第一轮就执行检索（0 次 LLM），模型第一次被调用时结果已在 transcript 里，
基于它直接作答——整条链路只保留一个回答回合。

边界（第一版窄场景）：
- 仅 conversation kind；SOP 需求由 sop_step_executor 场景 C 负责，不介入。
- 仅「新帧首轮」（transcript 为空）。同帧续轮/恢复不介入，避免重复检索。
- 仅「可用能力里没有 skill/general_skill/tool」的需求（knowledge_base /
  internal / file kind 不阻断：前者是授权描述，internal 是知识检索自身，
  file 是附件读取，与预填正交）。
- 打招呼/无关问题不做意图分类：检索是纯词法的（0.3s 级），误查成本可
  忽略——未命中（chunk_count=0）时模型看到空结果自然正常寒暄；模型在
  后续轮仍可自己调 knowledge_search 换词重搜（工具保留在 manifest 里，
  预填只是快路径不是唯一路径）。
- 任何异常返回空列表，静默回退现有 LLM 决策链路。
"""

from __future__ import annotations

from typing import Any

from app.llm import LLMClient

QUERY_MAX_CHARS = 200
# 与 knowledge_search 工具侧上限对齐（harness_capability_invoker min(…, 12)）
MAX_CHUNKS = 8
# 可用能力里出现这些 kind 说明不止知识库（有技能/工具要模型自己编排），不预填。
# CapabilityKind 全集 = {general_skill, knowledge, tool, file, internal}：
# knowledge 是授权描述、internal 是知识检索自身、file 是附件读取，均与预填正交。
_BLOCKING_KINDS = {"general_skill", "tool"}
TRACE_EVENT = "knowledge_prefetch_conversation"

JUDGE_SYSTEM_PROMPT = (
    "你是检索需求判定器。判断用户这条消息是否需要检索企业知识库（内部制度、"
    "项目资料、产品信息、业务数据等私有事实）才能回答。\n"
    "规则：\n"
    "- 打招呼、寒暄、感谢、告别、闲聊、测试性消息（如 hi/你好/在吗）→ no\n"
    "- 通用常识或时事问题，不涉及企业内部信息 → no\n"
    "- 涉及公司/项目/制度/产品/流程等具体事实，或上下文不明但像在问业务问题 → yes\n"
    '只输出 JSON：{"search": true} 或 {"search": false}'
)

# 纯寒暄词典快筛：命中直接跳过判定与预填（0 成本拦最明显的）。
# 只做「去标点/语气词后完全匹配」，绝不前缀匹配——「你好，我想问年假」不是寒暄。
_SMALLTALK_EXACT = {
    "hi", "hii", "hiii", "hello", "hey", "yo", "halo",
    "你好", "您好", "在吗", "在么", "在不在", "嗨", "哈喽", "哈罗",
    "hello there", "hi there",
}
_SMALLTALK_TRAILING = "呀哦哈呢吧嘛啊哟嘿~～!！?？。.，, 　"


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


def _is_smalltalk(message: str) -> bool:
    return _normalize_smalltalk(message) in _SMALLTALK_EXACT


def _judge_needs_search(
    query: str,
    judge_model: Any,
    trace_sink: Any,
) -> bool:
    """轻量模型判定「这条消息是否需要查知识库」。

    返回 True 才预填。异常/模型缺失一律返回 False——不确定性回退到
    现有 LLM 决策链路（模型自己决定调不调检索），绝不因为判定失败
    而强制检索。
    """

    if judge_model is None:
        return False
    import time as _time

    started = _time.monotonic()
    try:
        from app.observability.spans import llm_operation

        client = LLMClient(judge_model)
        # 专用 operation：output_policy 据此收紧重试；输出是纯 bool
        with llm_operation("knowledge.prefetch_judge"):
            raw = client.generate_json(
                JUDGE_SYSTEM_PROMPT, {"user_message": query[:QUERY_MAX_CHARS]}
            )
        needs = bool(raw.get("search")) if isinstance(raw, dict) else False
    except Exception:  # noqa: BLE001 - 判定失败回退现有链路
        needs = False
        if callable(trace_sink):
            try:
                trace_sink(
                    TRACE_EVENT + "_judge_error",
                    {"query": query[:QUERY_MAX_CHARS]},
                )
            except Exception:  # noqa: BLE001
                pass
    if callable(trace_sink):
        try:
            trace_sink(
                TRACE_EVENT + "_judge",
                {
                    "query": query[:QUERY_MAX_CHARS],
                    "needs_search": needs,
                    "duration_ms": round((_time.monotonic() - started) * 1000, 1),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return needs


def plan_knowledge_prefill_actions(
    requirement: Any,
    *,
    first_turn: bool = True,
    trace_sink: Any = None,
    judge_model: Any = None,
) -> list[dict[str, Any]]:
    """为仅知识库的 conversation 需求产出 knowledge_search 预填动作。

    不可判定/不满足窄场景条件时返回空列表，harness 侧静默回退现有链路。
    判定顺序：寒暄词典快筛（0 成本）→ 轻量模型二元判定（1-2s，准确率
    优先——误查一次 knowledge_search 要 15s 且给寒暄挂引用卡片）。
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
        # 判定 1：寒暄词典快筛（0 成本；只做去语气词后的完全匹配，
        # 「你好，我想问年假」这类混合消息不会命中）
        if _is_smalltalk(query):
            return []
        # 判定 2：轻量模型二元判定（准确率优先——误查一次 knowledge_search
        # 要 15s 且给寒暄挂一排引用卡片）；判定失败/否 → 不预填
        if not _judge_needs_search(query, judge_model, trace_sink):
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
