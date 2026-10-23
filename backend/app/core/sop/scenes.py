"""SOP 场景转移表（SceneContext + 六场景 + 动作构造）。

从 sop_step_executor 拆出的第二层。调度约定见 executor 的
``plan_sop_prefill_actions``：``SCENES`` 表序即优先级，场景 ``run`` 返回
None 表示未命中、继续下一表项，返回 list（含空）即终局。

本模块同时承载场景动作构造（询问/流转/转人工/预检索动作）与轻量槽位
抽取——场景类与它们同层互相调用。出边求值纯函数在
``app/core/sop_edge_eval.py``（依赖方向 executor → scenes → edge_eval）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.core.sop.slots.display import join_slot_labels, slot_label
from app.core.sop.slots.form import build_slot_form, is_confirm_field
from app.core.sop.edge_eval import (
    _EdgeCondition,
    _decision_direct_next,
    _edges_without_specs,
    _evaluate_edges,
    _knowledge_route_next,
    _last_knowledge_search_result,
    _last_result_ok,
    _route_direct_next,
    _unique_unconditional_next,
)

# trace 事件类型，供 diagnose 脚本统计 P1 生效率
TRACE_EVENT = "sop_prefill_planned"

_QUERY_MAX_CHARS = 200

# 槽位字段名归一（表单提交的键 → 声明字段）：``Employee-ID`` / ``employeeId``
# 都要能对上 ``employee_id``。
_FIELD_SPLIT = re.compile(r"[^0-9a-z]+")
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# P3 场景 D/E：节点类型门控（与 skill_schema.SkillGraphNode.type 对齐）
_DECISION_NODE_TYPE = "decision"
_TOOL_CALL_NODE_TYPE = "tool_call"
# 场景 F：人工处理终点节点
_HANDOFF_NODE_TYPE = "handoff"

# 场景 E：内置能力不走参数直组（有自己的执行通道/渐进披露协议）
_INTERNAL_TOOL_NAMES = {
    "capability_search",
    "capability_describe",
    "knowledge_search",
    "list_published_deliverables",
    "read_published_deliverable",
}


@dataclass
class SceneContext:
    """一次 plan 调用的共享上下文。

    轻量视图（槽位/能力/出边声明）直接从 requirement 派生；涉及 LLM 的重
    推导（A2 新鲜抽取）与派生视图（``merged_slots`` / ``edge_conditions``）
    做成**惰性 memoize**——只有 F/D/G/B 场景会触碰，保证 C/E/A 命中时不会
    多出一次抽取 LLM 调用（与改造前阶梯的执行位置严格对齐）。
    """

    requirement: Any
    step: dict[str, Any]
    sop_context: dict[str, Any]
    resume_slot_recovery: bool
    satisfied_required_knowledge_ids: set[str] | None
    trace_sink: Any
    slot_extraction_model: Any
    edge_condition_specs: Mapping[str, Any] | None
    slot_submission: Mapping[str, Any] | None
    # 场景 L：Laya 出边决策旁路（未启用/影子模式时为 None 或 enabled=False）
    laya_router: Any = None

    _fresh_slots: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _merged_slots: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _edge_conditions: list[_EdgeCondition] | None = field(
        default=None, init=False, repr=False
    )

    # -- 轻量视图（纯派生，无副作用） -------------------------------------

    @property
    def slot_fields(self) -> list[str]:
        # 技能全图声明的槽位字段（编译期从各节点 expected_user_info 并集而来），
        # 作为路由证据/回复摘要的字段白名单，隔离会话历史遗留的脏槽位。
        return [
            str(item).strip()
            for item in (self.sop_context.get("slot_fields") or [])
            if str(item).strip()
        ]

    @property
    def slot_labels(self) -> Mapping[str, Any]:
        # 字段显示名（技能自报，内置词典兜底）——只用于拼**用户可见回复**，
        # 不参与任何判定。缺省时全部走内置词典 / 原样返回。
        declared = self.sop_context.get("slot_labels")
        return declared if isinstance(declared, Mapping) else {}

    @property
    def required_slots(self) -> list[str]:
        return [str(item) for item in getattr(self.requirement, "required_slots", []) or []]

    @property
    def required_capabilities(self) -> list[str]:
        return [
            str(item)
            for item in getattr(self.requirement, "required_capability_names", []) or []
        ]

    @property
    def required_knowledge_ids(self) -> list[str]:
        return [
            str(item)
            for item in getattr(self.requirement, "required_knowledge_base_ids", []) or []
        ]

    @property
    def known_slots(self) -> dict[str, Any]:
        return dict(getattr(self.requirement, "known_slots", {}) or {})

    @property
    def source_message(self) -> str:
        return str(getattr(self.requirement, "source_user_message", "") or "")

    @property
    def allowed_transitions(self) -> list[Any]:
        return list(getattr(self.requirement, "allowed_transitions", []) or [])

    @property
    def step_type(self) -> str:
        return str(self.step.get("type") or "").strip()

    @property
    def skill_id(self) -> str:
        return str(self.sop_context.get("skill_id") or "")

    # -- 惰性重推导（F/D/G/B 专属，含一次轻量抽取 LLM） -------------------

    @property
    def fresh_slots(self) -> dict[str, Any]:
        if self._fresh_slots is None:
            self._fresh_slots = self._extract_fresh_slots()
        return self._fresh_slots

    def _extract_fresh_slots(self) -> dict[str, Any]:
        # 场景 A2：会话复用的新鲜度保护。required_slots 为空只说明
        # session.slots_json 里有值——可能是**上一轮请求**的旧值（同会话
        # 第二次申请换了工号，17:41 复测踩中：旧工号1003 带着走完全程）。
        # 当前节点声明了 expected_user_info 且有新用户消息时跑一次轻量抽取，
        # 新值合并进路由证据（merged_slots）并随 B 直通的 slot_updates 落库
        # 覆盖旧值。
        if (
            self.required_slots
            or self.resume_slot_recovery
            or self.slot_extraction_model is None
        ):
            return {}
        fresh_expected = [
            str(item).strip()
            for item in (self.step.get("expected_user_info") or [])
            if str(item).strip()
        ]
        if not fresh_expected or not self.source_message.strip():
            return {}
        extracted = _extract_slots_llm(
            fresh_expected,
            self.source_message,
            self.known_slots,
            self.slot_extraction_model,
            self.trace_sink,
            step_instruction=str(self.step.get("instruction") or ""),
            mode="fresh_merge",
        )
        return {
            str(key): value
            for key, value in extracted.items()
            if str(key) in set(fresh_expected) and value not in (None, "", [], {})
        }

    @property
    def merged_slots(self) -> dict[str, Any]:
        if self._merged_slots is None:
            fresh = self.fresh_slots
            self._merged_slots = (
                {**self.known_slots, **fresh} if fresh else self.known_slots
            )
        return self._merged_slots

    @property
    def edge_conditions(self) -> list[_EdgeCondition]:
        """出边的结构化求值（场景 G 的证据）：一次算好，供 F/D/G/B 复用。

        找不到任何编译产物时 spec 全为 None，等价于改造前的行为。
        """
        if self._edge_conditions is None:
            self._edge_conditions = _evaluate_edges(
                self.allowed_transitions,
                step=self.step,
                specs=self.edge_condition_specs,
                slots=self.merged_slots,
                user_message=self.source_message,
                allowed_fields=set(self.slot_fields),
                last_result_ok=_last_result_ok(self.requirement),
            )
        return self._edge_conditions


def _plan_prefetch_or_passthrough(
    requirement: Any,
    *,
    required_knowledge_ids: list[str],
    satisfied_required_knowledge_ids: set[str] | None,
    extracted_slots: dict[str, Any],
    step: dict[str, Any],
    trace_sink: Any,
    scene_suffix: str,
    edge_conditions: list[_EdgeCondition] | None = None,
) -> list[dict[str, Any]]:
    """槽位无缺口后的确定性分支：C 预检索 → G 条件求值直判 → B 纯流转直通。

    场景 A 抽齐槽位后也走这里（scene_suffix="_after_extraction" 供观测
    区分抽取后直通的次数），并用抽出后的完整槽位重算 ``edge_conditions``，
    因此「槽位齐了就该走某条条件分支」也能被场景 G 直接判定。

    ``edge_conditions`` 为空时按「未编译」处理（只认无条件边），与改造前
    的行为完全一致。
    """

    # C：预检索
    if required_knowledge_ids:
        already = satisfied_required_knowledge_ids or set()
        pending_kb_ids = [kb for kb in required_knowledge_ids if kb not in already]
        query = str(getattr(requirement, "source_user_message", "") or "").strip()
        if pending_kb_ids:
            if not query:
                return []
            action = _knowledge_search_action(query, pending_kb_ids)
            return _emit(
                trace_sink,
                f"prefetch_knowledge{scene_suffix}",
                [action],
                query=query[:_QUERY_MAX_CHARS],
                knowledge_base_ids=pending_kb_ids,
            )
        # C2：全部知识库检索完成的重入帧。出边条件只以检索结果为自变量时
        # （「检索完成」→继续 /「无法检索」→转人工），按 knowledge_search
        # 返回直接判定，整帧零 LLM——业务判断留给后续 decision 节点。
        direct_next, outcome, miss_reason = _knowledge_route_next(
            _edges_without_specs(list(getattr(requirement, "allowed_transitions", []) or [])),
            *_last_knowledge_search_result(requirement),
        )
        if direct_next:
            action = _passthrough_action(step, direct_next)
            return _emit(
                trace_sink,
                f"knowledge_direct_route{scene_suffix}",
                [action],
                next_node_id=direct_next,
                outcome=outcome,
            )
        if miss_reason:
            return _emit(
                trace_sink,
                f"knowledge_direct_route_skipped{scene_suffix}",
                [],
                reason=miss_reason,
            )
        return []

    edges = (
        edge_conditions
        if edge_conditions is not None
        else _edges_without_specs(list(getattr(requirement, "allowed_transitions", []) or []))
    )

    # G：结构化条件求值直判（零 LLM）。
    if any(item.has_spec for item in edges):
        spec_next = _route_direct_next(edges)
        if spec_next:
            action = _passthrough_action(step, spec_next)
            if extracted_slots:
                action["slot_updates"] = extracted_slots
            return _emit(
                trace_sink,
                f"condition_spec_direct_eval{scene_suffix}",
                [action],
                next_node_id=spec_next,
                kinds=[item.kind for item in edges if item.has_spec],
            )
        # 图上有编译产物、但无法唯一判定（多条命中 / 存在未知边）→ **整段交还
        # LLM**，绝不继续走 B：B 会挑中那条 `always` 兜底边，等于在没排除其它
        # 条件分支的前提下擅自走了 else，分支语义就错了。
        return []

    # B：纯流转直通（未编译的图——历史行为完全不变；抽取值随 slot_updates 落库）
    next_node_id = _unique_unconditional_next(edges)
    if next_node_id:
        action = _passthrough_action(step, next_node_id)
        if extracted_slots:
            action["slot_updates"] = extracted_slots
        return _emit(
            trace_sink,
            f"passthrough_transition{scene_suffix}",
            [action],
            next_node_id=next_node_id,
            extracted_slots=extracted_slots,
        )
    return []


def _plan_tool_direct(
    requirement: Any,
    *,
    step: dict[str, Any],
    required_capabilities: list[str],
    required_slots: list[str],
    required_knowledge_ids: list[str],
    known_slots: dict[str, Any],
    trace_sink: Any,
) -> list[dict[str, Any]]:
    """场景 E：tool_call 节点按 input_schema 从槽位直组参数。

    required 能力在编译期已随 manifest 激活（initially_activated_names），
    直接产出 tool 动作即可调用，省掉 capability_describe + LLM 组参两轮。
    schema 必填参数无法全部由槽位满足时放弃（观测 skipped 原因）。
    """

    name = ""
    try:
        if required_slots or required_knowledge_ids:
            return []
        if str(step.get("type") or "").strip() != _TOOL_CALL_NODE_TYPE:
            return []
        tool_caps = [
            item for item in required_capabilities if item not in _INTERNAL_TOOL_NAMES
        ]
        if len(tool_caps) != 1:
            return []
        name = tool_caps[0]
        descriptor = None
        for item in getattr(requirement, "capability_manifest", None).available or []:
            if getattr(item, "name", "") == name and getattr(item, "available", False):
                descriptor = item
                break
        if descriptor is None:
            return []
        schema = getattr(descriptor, "input_schema", None)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict) or not properties:
            return []
        schema_required = schema.get("required")
        schema_required = (
            [str(item) for item in schema_required]
            if isinstance(schema_required, list)
            else []
        )
        arguments = {
            str(prop): value
            for prop, value in known_slots.items()
            if prop in properties and value not in (None, "", [], {})
        }
        missing = [prop for prop in schema_required if prop not in arguments]
        if missing:
            return _emit(
                trace_sink,
                "tool_direct_call_skipped",
                [],
                tool_name=name,
                missing_required=missing,
            )
        step_name = str(step.get("name") or "").strip()
        action = {
            "action": "tool",
            "tool_name": name,
            "arguments": arguments,
            "task_summary": (
                f"SOP 工具节点「{step_name}」按槽位直组参数调用 {name}（跳过 LLM 决策轮）。"
            ),
        }
        return _emit(
            trace_sink,
            "tool_direct_call",
            [action],
            tool_name=name,
            argument_keys=sorted(arguments),
        )
    except Exception:  # noqa: BLE001 - 执行器绝不阻断主链路
        return []


EXTRACTION_SYSTEM_PROMPT = (
    "你是工单槽位抽取器。任务：从用户消息中为指定字段提取取值。"
    "字段名是 snake_case 抽象标识，field_context 是当前步骤的业务说明，"
    "用它理解每个字段收集的是什么信息。"
    "规则：1) 只提取用户消息中明确表达的信息，严禁编造或推测；"
    "2) 一段口语描述可以拆开映射到多个字段"
    "（如「生产环境的管理员权限」可拆为 权限级别=管理员、环境/访问级别=生产环境）；"
    "3) 某字段在消息中没有明确取值就省略该字段；"
    "4) 取值沿用用户消息中的原始词汇，不要翻译或改写"
    "（如「普通权限」就写 普通权限，不要归一化成 normal）。"
    "只输出 JSON 对象，形如 {'slot_values': {'字段名': '取值'}}。"
)


def _extract_slots_llm(
    fields: list[str],
    user_message: str,
    known_slots: dict[str, Any],
    model_config: Any,
    trace_sink: Any,
    *,
    step_instruction: str = "",
    mode: str = "missing_fill",
) -> dict[str, Any]:
    """轻量槽位抽取：从用户首条消息中提取期望字段值。

    P2 核心：原 task_action 链路里这层抽取混在 25-110s 的大 payload 决策轮，
    这里拆成独立的 1-3s 轻量调用。失败/不可用时返回空 dict，调用方退回
    模板询问（保守行为）。step_instruction 是 SOP 节点说明，为抽象字段名
    提供业务语义（如 access_level=访问级别/环境类型）。
    mode：missing_fill=缺槽补全（场景 A）/ fresh_merge=会话复用新值合并（A2）。
    """

    if model_config is None or not fields or not user_message.strip():
        return {}
    import time as _time

    started = _time.monotonic()
    result: dict[str, Any] = {}
    try:
        from app.llm import LLMClient
        from app.observability.spans import llm_operation

        client = LLMClient(model_config)
        payload = {
            "fields": list(fields),
            "field_context": str(step_instruction or "")[:600],
            "known_slots": known_slots,
            "user_message": user_message[:2_000],
        }
        # 专用 operation：output_policy 据此收紧 json 修复/空响应重试
        # （默认 3 次修复在慢模型上会串行烧 2-4 轮完整 LLM 调用）
        with llm_operation("sop.slot_extraction"):
            raw = client.generate_json(EXTRACTION_SYSTEM_PROMPT, payload)
        values = raw.get("slot_values") if isinstance(raw, dict) else None
        result = dict(values) if isinstance(values, dict) else {}
    except Exception:  # noqa: BLE001 - 抽取失败退回模板询问，绝不阻断
        result = {}
    if callable(trace_sink):
        try:
            trace_sink(
                "sop_slot_extraction",
                {
                    "mode": mode,
                    "fields": list(fields),
                    "extracted": result,
                    "duration_ms": round((_time.monotonic() - started) * 1000, 1),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return result


def _knowledge_search_action(query: str, knowledge_base_ids: list[str]) -> dict[str, Any]:
    return {
        "action": "tool",
        "tool_name": "knowledge_search",
        "arguments": {
            "query": query[:_QUERY_MAX_CHARS],
            "knowledge_base_ids": knowledge_base_ids,
            "max_chunks": 8,
        },
        "task_summary": "SOP 确定性预检索（跳过 LLM 决策轮）。",
    }


def _passthrough_action(step: dict[str, Any], next_node_id: str) -> dict[str, Any]:
    step_name = str(step.get("name") or step.get("node_id") or "").strip()
    return {
        "action": "finish",
        "status": "completed",
        "next_step_id": next_node_id,
        "reply_fragment": "",
        "task_summary": f"SOP 步骤「{step_name}」无待办事项，确定性流转到下一节点。",
    }


def _plan_handoff_passthrough(
    step: dict[str, Any],
    known_slots: dict[str, Any],
    trace_sink: Any,
    *,
    allowed_fields: set[str] | None = None,
    labels: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """场景 F：handoff 终点节点直通，零 LLM 发起转人工。

    status 直接置 handoff（引擎据 result.status 建人工请求并挂起会话）；
    回复模板带槽位摘要，只展示技能声明字段（slot_fields）——历史遗留
    的脏槽位（如上一轮的 permission_preference）不会出现在用户回复里。
    摘要里的字段名走 slot_display（中文显示名），用户看到的是
    「员工工号：1001」而不是「employee_id：1001」。
    """
    allowed = {str(item).strip() for item in (allowed_fields or set()) if str(item).strip()}
    filled = {
        str(key): value
        for key, value in (known_slots or {}).items()
        if (not allowed or str(key).strip() in allowed)
        and value not in (None, "", [], {})
    }
    summary = "；".join(
        f"{slot_label(key, labels)}：{value}" for key, value in filled.items()
    )
    reply = (
        f"已根据您提供的信息（{summary}）发起人工处理，会有工作人员跟进。"
        if summary
        else "已发起人工处理，会有工作人员跟进，请稍候。"
    )
    step_name = str(step.get("name") or step.get("node_id") or "").strip()
    action = {
        "action": "finish",
        "status": "handoff",
        "reply_fragment": reply,
        "task_summary": f"SOP 步骤「{step_name}」为人工处理终点，确定性发起转人工。",
        "slot_updates": {},
    }
    return _emit(
        trace_sink,
        "handoff_passthrough",
        [action],
        known_slots_count=len(filled),
    )


def _submission_slots(
    submission: Mapping[str, Any] | None,
    *,
    allowed_fields: set[str],
) -> dict[str, Any]:
    """表单提交值 → 槽位字典。

    保守口径与 LLM 抽取一致：只收**本节点声明过**的字段，空值不算「已提供」
    （留空 = 没回答，仍要继续问）。字段名做一次大小写/连字符归一匹配，容忍
    前端把 ``employee_id`` 写成 ``employeeId``。
    """

    if not isinstance(submission, Mapping) or not submission:
        return {}
    if not allowed_fields:
        return {}
    lookup = {_field_key(item): item for item in allowed_fields}
    result: dict[str, Any] = {}
    for raw_key, raw_value in submission.items():
        key = lookup.get(_field_key(raw_key))
        if not key:
            continue
        value: Any = raw_value
        if isinstance(value, str):
            value = value.strip()
        if value in (None, "", [], {}):
            continue
        result[key] = value
    return result


def _field_key(name: Any) -> str:
    text = _CAMEL_SPLIT.sub("_", str(name or ""))
    tokens = [token for token in _FIELD_SPLIT.split(text.lower()) if token]
    return "_".join(tokens)


def _await_user_action(
    step: dict[str, Any],
    required_slots: list[str],
    *,
    labels: Mapping[str, Any] | None = None,
    edge_conditions: list[_EdgeCondition] | None = None,
    skill_id: str = "",
) -> dict[str, Any]:
    step_name = str(step.get("name") or "").strip()
    # 只问调用方传入的缺失字段（expected_user_info 里可能有些字段已满足，
    # 全量罗列会重现「已提供还要求补全」的错误询问）
    missing = [str(item).strip() for item in required_slots if str(item).strip()]
    step_part = f"继续「{step_name}」" if step_name else "继续当前步骤"
    # 确认型步骤（缺的全部是 confirm 字段）：这一步的全部意义就是让用户点
    # 一下确认——话术也按确认写，别再出现「请提供：确认操作」这种要求用户
    # 手打「确认」二字的句子。按钮由 A2UI 表单下发；企微等纯文本渠道按
    # 「回复确认/取消」理解即可，两边口径一致。
    if missing and all(is_confirm_field(item) for item in missing):
        target = f"「{step_name}」" if step_name else "以上内容"
        reply = f"请确认{target}：确认请点「确认」继续，需修改请点「取消」返回。"
    else:
        reply = f"为了{step_part}，请提供：{join_slot_labels(missing, labels)}。"
    # A2UI（MVP）：同一批缺失字段再编译一份**表单描述**随消息下发，前端渲染成
    # 原生控件。用户提交的是结构化 JSON → 下一轮直接写槽，省掉 _extract_slots_llm。
    # 文案仍然保留：企微/微信等非 UI 渠道只能收文本。
    ui_form = build_slot_form(
        missing,
        labels=labels,
        edge_conditions=edge_conditions,
        step_name=step_name,
        step_id=str(step.get("node_id") or step.get("step_id") or "").strip(),
        skill_id=skill_id,
    )
    return {
        "action": "finish",
        "status": "awaiting_user",
        "reply_fragment": reply,
        "task_summary": "SOP 步骤缺少必填信息，确定性发起用户询问。",
        "slot_updates": {},
        "ui_form": ui_form,
    }


def _emit(
    trace_sink: Any,
    scene: str,
    actions: list[dict[str, Any]],
    **details: Any,
) -> list[dict[str, Any]]:
    if callable(trace_sink):
        try:
            trace_sink(
                TRACE_EVENT,
                {
                    "scene": scene,
                    "action_count": len(actions),
                    "actions": [item.get("action") for item in actions],
                    **details,
                },
            )
        except Exception:  # noqa: BLE001 - 观测失败不影响主链路
            pass
    return actions



class CPrefetch:
    """场景 C：预检索——节点声明强制知识库且槽位无缺口时直接产出
    knowledge_search 动作，跳过「LLM 决策调检索」那一轮。
    恢复补槽会话不做预检索，避免与已完成的检索重复。"""

    name = "C_prefetch"

    @classmethod
    def guard(cls, ctx: SceneContext) -> bool:
        return (
            not ctx.resume_slot_recovery
            and bool(ctx.required_knowledge_ids)
            and not ctx.required_slots
        )

    @classmethod
    def run(cls, ctx: SceneContext) -> list[dict[str, Any]]:
        return _plan_prefetch_or_passthrough(
            ctx.requirement,
            required_knowledge_ids=ctx.required_knowledge_ids,
            satisfied_required_knowledge_ids=ctx.satisfied_required_knowledge_ids,
            extracted_slots={},
            step=ctx.step,
            trace_sink=ctx.trace_sink,
            scene_suffix="",
        )


class EToolDirect:
    """场景 E：tool_call 节点按 input_schema 从槽位直组参数——required
    能力在编译期已随 manifest 激活，省掉 capability_describe + LLM 组参
    两轮。命中即终局（放弃时返回空列表，不落 G/B）。"""

    name = "E_tool_direct"

    @classmethod
    def guard(cls, ctx: SceneContext) -> bool:
        return bool(ctx.required_capabilities or ctx.required_knowledge_ids)

    @classmethod
    def run(cls, ctx: SceneContext) -> list[dict[str, Any]]:
        return _plan_tool_direct(
            ctx.requirement,
            step=ctx.step,
            required_capabilities=ctx.required_capabilities,
            required_slots=ctx.required_slots,
            required_knowledge_ids=ctx.required_knowledge_ids,
            known_slots=ctx.known_slots,
            trace_sink=ctx.trace_sink,
        )


class ASlotFill:
    """场景 A（P2 增强）：缺槽时先尝试轻量 LLM 槽位抽取——用户首条消息
    往往已带齐信息（如「申请OA管理员权限，工号3012」），编译期
    required_slots 只看 session.slots_json 会误判缺槽。抽齐后落 C/C2/G/B；
    部分/未抽到则确定性发起询问（A2UI 表单 + 模板文案）。"""

    name = "A_slot_fill"

    @classmethod
    def guard(cls, ctx: SceneContext) -> bool:
        return bool(ctx.required_slots)

    @classmethod
    def run(cls, ctx: SceneContext) -> list[dict[str, Any]]:
        required_slots = ctx.required_slots
        expected_fields = [
            str(item).strip()
            for item in (ctx.step.get("expected_user_info") or [])
            if str(item).strip()
        ]
        allowed_extract_fields = set(expected_fields or required_slots)
        # A2UI：用户直接在表单里提交了结构化值 → 不再让模型去猜文本。
        # 这是整条链路里唯一一处「确定性拿到槽位」的机会，抽取值口径与
        # LLM 抽取完全一致（同一份字段白名单 + 同样的非空过滤）。
        submitted = _submission_slots(
            ctx.slot_submission,
            allowed_fields=allowed_extract_fields,
        )
        if submitted:
            extracted = submitted
        else:
            extracted = _extract_slots_llm(
                expected_fields or required_slots,
                ctx.source_message,
                ctx.known_slots,
                ctx.slot_extraction_model,
                ctx.trace_sink,
                step_instruction=str(ctx.step.get("instruction") or ""),
            )
            extracted = {
                str(key): value
                for key, value in extracted.items()
                if str(key) in allowed_extract_fields
                and value not in (None, "", [], {})
            }
        missing = [slot for slot in required_slots if slot not in extracted]
        if not missing:
            # 抽齐：槽位缺口解除 → 用**抽出后的完整槽位**重算条件边，
            # 再走 C/C2/G/B 判定（「信息齐了就走 X 分支」这类最常见条件正
            # 是在这里被直判掉的；缺了这一步它只能回落 LLM）。
            return _plan_prefetch_or_passthrough(
                ctx.requirement,
                required_knowledge_ids=ctx.required_knowledge_ids,
                satisfied_required_knowledge_ids=ctx.satisfied_required_knowledge_ids,
                extracted_slots=extracted,
                step=ctx.step,
                trace_sink=ctx.trace_sink,
                scene_suffix="_after_extraction",
                edge_conditions=_evaluate_edges(
                    ctx.allowed_transitions,
                    step=ctx.step,
                    specs=ctx.edge_condition_specs,
                    slots={**ctx.known_slots, **extracted},
                    user_message=ctx.source_message,
                    allowed_fields=set(ctx.slot_fields),
                    last_result_ok=_last_result_ok(ctx.requirement),
                ),
            )
        # 部分/未抽到：问缺的，已抽到的值随 slot_updates 落库防重复问
        action = _await_user_action(
            ctx.step,
            missing,
            labels=ctx.slot_labels,
            edge_conditions=ctx.edge_conditions,
            skill_id=ctx.skill_id,
        )
        action["slot_updates"] = extracted
        return _emit(
            ctx.trace_sink,
            "await_user_for_slots",
            [action],
            required_slots=missing,
            extracted_slots=extracted,
        )


class FHandoff:
    """场景 F（P4）：handoff 终点节点直通。槽位无缺口、无强制能力/知识、
    无出边（终点）时，转人工是纯协议动作——模板回复带槽位摘要即可，
    跳过纯协议 task_action LLM 轮（实测 12-17s/轮，转交节点连烧 3 轮）。"""

    name = "F_handoff"

    @classmethod
    def guard(cls, ctx: SceneContext) -> bool:
        return ctx.step_type == _HANDOFF_NODE_TYPE and not ctx.allowed_transitions

    @classmethod
    def run(cls, ctx: SceneContext) -> list[dict[str, Any]]:
        return _plan_handoff_passthrough(
            ctx.step,
            ctx.merged_slots,
            ctx.trace_sink,
            allowed_fields=set(ctx.slot_fields),
            labels=ctx.slot_labels,
        )


class DDecision:
    """场景 D（P3，场景 G 的兜底）：decision 节点的分支条件能用槽位取值
    直接证定时跳过 LLM 决策轮。**只在这些边一条都没编译过时启用**——已有
    结构化条件的边由场景 G 说了算，两套判据混用会互相打架。
    未命中返回 None，继续匹配下一表项（G/B）。"""

    name = "D_decision"

    @classmethod
    def guard(cls, ctx: SceneContext) -> bool:
        return ctx.step_type == _DECISION_NODE_TYPE and not any(
            item.has_spec for item in ctx.edge_conditions
        )

    @classmethod
    def run(cls, ctx: SceneContext) -> list[dict[str, Any]] | None:
        next_target = _decision_direct_next(
            ctx.allowed_transitions,
            ctx.merged_slots,
            user_message=ctx.source_message,
            allowed_fields=set(ctx.slot_fields),
        )
        if not next_target:
            return None
        action = _passthrough_action(ctx.step, next_target)
        if ctx.fresh_slots:
            # A2 新值随直判落库，避免旧槽位（如旧工号）继续存活
            action["slot_updates"] = ctx.fresh_slots
        return _emit(
            ctx.trace_sink,
            "decision_direct_eval",
            [action],
            next_node_id=next_target,
        )


class LayaEdgeDecision:
    """场景 L：确定性求值判不了时，用 Laya 决策头做一次出边抉择。

    放在 D 之后、G/B 之前。guard 已复刻场景 G 的前提（能确定性唯一命中
    就不打扰 Laya），所以这里只需处理「真正需要语义判断」的边。
    影子模式返回 None（不改路由），只把判定结果写进 trace，供先看一致率。
    """

    name = "L_laya_edge_decision"

    @classmethod
    def guard(cls, ctx: SceneContext) -> bool:
        router = ctx.laya_router
        if router is None or not getattr(router, "enabled", False):
            return False
        # 缺槽由场景 A 负责；必检索未完成不能抢在检索前选路
        if ctx.required_slots:
            return False
        satisfied = ctx.satisfied_required_knowledge_ids or set()
        if any(kb not in satisfied for kb in ctx.required_knowledge_ids):
            return False
        edges = ctx.edge_conditions
        # 没有条件边（只有无条件边）→ 交给 B；能唯一判定 → 交给 G，都不用 Laya
        if not any(not item.is_always for item in edges):
            return False
        if _route_direct_next(edges):
            return False
        return True

    @classmethod
    def run(cls, ctx: SceneContext) -> list[dict[str, Any]] | None:
        router = ctx.laya_router
        outcome = router.route_edges(ctx.requirement, ctx.edge_conditions, ctx.step)
        if outcome is None:
            return None
        shadow = bool(getattr(router, "shadow", False))
        decision = outcome.decision
        actions: list[dict[str, Any]] = []
        if decision is not None and not shadow:
            action = _passthrough_action(ctx.step, decision.next_node_id)
            if ctx.fresh_slots:
                # A2 新值随直判落库，避免旧槽位继续存活（与 D/G/B 同口径）
                action["slot_updates"] = ctx.fresh_slots
            actions = [action]
        _emit(
            ctx.trace_sink,
            "laya_edge_decision",
            actions,
            shadow=shadow,
            **outcome.as_trace_payload(),
        )
        # 影子模式 / 未命中 / 低置信 → 返回 None，继续走 G/B 与主模型 LLM
        return actions or None


class GBPassthrough:
    """场景 G（结构化条件求值直判，零 LLM）+ 场景 B（纯流转直通）——
    兜底表项。A2 抽到的新值随 slot_updates 落库覆盖旧值。"""

    name = "G_B_passthrough"

    @classmethod
    def guard(cls, ctx: SceneContext) -> bool:
        return True

    @classmethod
    def run(cls, ctx: SceneContext) -> list[dict[str, Any]]:
        return _plan_prefetch_or_passthrough(
            ctx.requirement,
            required_knowledge_ids=[],
            satisfied_required_knowledge_ids=ctx.satisfied_required_knowledge_ids,
            extracted_slots=ctx.fresh_slots,
            step=ctx.step,
            trace_sink=ctx.trace_sink,
            scene_suffix="",
            edge_conditions=ctx.edge_conditions,
        )


# 有序场景转移表：表序即优先级。新增场景 = 定义一个场景类并追加到此；
# 「C/E/A 命中即终局、D 未命中落 G/B」的续接关系由 run 的返回值约定表达。
SCENES: tuple[type, ...] = (
    CPrefetch,
    EToolDirect,
    ASlotFill,
    FHandoff,
    DDecision,
    LayaEdgeDecision,
    GBPassthrough,
)


def _validate_scenes() -> None:
    """加载期自检：guard/run/name 缺失或重复在 import 时即报错，
    不留到调度时被 executor 的兜底 except 静默吞掉。"""
    seen: set[str] = set()
    for scene in SCENES:
        assert callable(getattr(scene, "guard", None)), f"{scene.__name__} 缺 guard"
        assert callable(getattr(scene, "run", None)), f"{scene.__name__} 缺 run"
        name = str(getattr(scene, "name", "") or "")
        assert name, f"{scene.__name__} 缺 name"
        assert name not in seen, f"场景 name 重复: {name}"
        seen.add(name)


_validate_scenes()


