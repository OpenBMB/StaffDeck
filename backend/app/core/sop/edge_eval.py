"""SOP 出边求值层（纯函数，无 LLM、无动作构造）。

从 sop_step_executor 拆出的第三层：把「边条件编译产物对齐、结构化求值、
knowledge_search 结果直判词族、decision 词元证据匹配」这些**只读判定**
集中在此，供 ``app/core/sop_scenes.py`` 的场景 guard/run 复用。
依赖方向固定：

    sop_step_executor → sop_scenes → sop_edge_eval

本模块内的函数/常量是包内私有约定（下划线前缀），不对外承诺稳定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from app.skills.edge_condition_spec import (
    EdgeConditionSpec,
    EdgeEvalContext,
    condition_fingerprint,
    evaluate_edge_condition,
    spec_from_payload,
)

# 允许确定性直通的出边条件（与 GraphRules.edge_condition 的空值语义一致）
_UNCONDITIONAL_EDGE_CONDITIONS = {"", "default", "else"}

# 场景 D：含否定语义的分支不做证据匹配（「非高权限」无法用正向包含判定）
_NEGATION_MARKERS = ("非", "不", "排除", "除外", "无法")
_CONDITION_TOKEN_SPLIT = re.compile(r"[/、，,;；\s]+")
# 双向包含匹配的最小词元长度（过滤「权」这类单字误配）
_MIN_TOKEN_CHARS = 2


@dataclass(frozen=True)
class _EdgeCondition:
    """一条出边 + 它的结构化条件（未编译时为 ``None``）与求值结果。"""

    target: str
    condition_text: str
    spec: EdgeConditionSpec | None
    value: bool | None

    @property
    def kind(self) -> str:
        return self.spec.kind if self.spec is not None else ""

    @property
    def has_spec(self) -> bool:
        return self.spec is not None

    @property
    def is_always(self) -> bool:
        """无条件边：编译后是 ``always``，或未编译但条件文本就是空/default/else。"""

        if self.kind == "always":
            return True
        if self.spec is not None:
            return False
        return self.condition_text.strip().lower() in _UNCONDITIONAL_EDGE_CONDITIONS


def _evaluate_edges(
    allowed_transitions: list[Any],
    *,
    step: dict[str, Any],
    specs: Mapping[str, Any] | None,
    slots: Mapping[str, Any],
    user_message: str,
    allowed_fields: set[str],
    last_result_ok: bool | None,
) -> list[_EdgeCondition]:
    """把当前节点的出边与结构化条件对齐并求值。

    证据口径与场景 D 一致：槽位只取技能声明的字段（``allowed_fields``），
    隔离会话历史遗留的脏槽位。
    """

    source_node_id = str(step.get("node_id") or step.get("step_id") or "").strip()
    required_fields = [
        str(item).strip()
        for item in (step.get("expected_user_info") or [])
        if str(item).strip()
    ]
    scoped_slots = {
        str(key): value
        for key, value in (slots or {}).items()
        if not allowed_fields or str(key).strip() in allowed_fields
    }
    context = EdgeEvalContext(
        slots=scoped_slots,
        node_required_fields=tuple(required_fields),
        user_message=user_message,
        last_result_ok=last_result_ok,
    )
    edges: list[_EdgeCondition] = []
    for edge in allowed_transitions:
        if not isinstance(edge, dict):
            continue
        target = str(edge.get("next_node_id") or "").strip()
        if not target:
            continue
        spec = _spec_for_edge(
            edge,
            source_node_id=source_node_id,
            target=target,
            required_fields=required_fields,
            specs=specs,
        )
        edges.append(
            _EdgeCondition(
                target=target,
                condition_text=str(edge.get("condition") or "").strip(),
                spec=spec,
                value=evaluate_edge_condition(spec, context),
            )
        )
    return edges


def _spec_for_edge(
    edge: dict[str, Any],
    *,
    source_node_id: str,
    target: str,
    required_fields: list[str],
    specs: Mapping[str, Any] | None,
) -> EdgeConditionSpec | None:
    """取一条边的结构化条件：边内嵌优先，其次按指纹查编译产物表。"""

    embedded = spec_from_payload(edge.get("condition_spec"))
    if embedded is not None:
        return embedded
    if not specs or not source_node_id:
        return None
    fingerprint = condition_fingerprint(
        source_node_id,
        target,
        edge.get("condition"),
        required_fields,
    )
    return spec_from_payload(specs.get(fingerprint))


def _edges_without_specs(allowed_transitions: list[Any]) -> list[_EdgeCondition]:
    """未接入编译产物时的降级视图（等价于改造前的「只看无条件边」）。"""

    edges: list[_EdgeCondition] = []
    for edge in allowed_transitions:
        if not isinstance(edge, dict):
            continue
        target = str(edge.get("next_node_id") or "").strip()
        if not target:
            continue
        edges.append(
            _EdgeCondition(
                target=target,
                condition_text=str(edge.get("condition") or "").strip(),
                spec=None,
                value=None,
            )
        )
    return edges


def _last_result_ok(requirement: Any) -> bool | None:
    """上一步能力/检索调用是否成功（供 ``result_ok`` / ``result_failed`` 求值）。

    取 ``prior_task_results`` 里**最近一条带能力调用结果**的记录；没有则返回
    ``None``——「没有结果可依据」与「结果失败」是两回事，后者会让模型辛苦
    建立的错误分支被错误地当成命中。
    """

    results = getattr(requirement, "prior_task_results", None) or []
    for item in reversed(list(results)):
        if not isinstance(item, dict):
            continue
        capability_results = item.get("capability_results")
        if not isinstance(capability_results, list) or not capability_results:
            continue
        last = capability_results[-1]
        if not isinstance(last, dict) or "success" not in last:
            return None
        return bool(last.get("success"))
    return None


# 场景 C2 词族：出边条件只认这两类「检索结果自变量」。「检索失败」词族
# 优先匹配（「未检索到」字面包含「检索到」，顺序反了会把失败边归成成功边）。
_KNOWLEDGE_ROUTE_FAILURE_MARKERS = (
    "无法检索",
    "检索失败",
    "检索不到",
    "未检索到",
    "未能检索",
    "检索异常",
)
_KNOWLEDGE_ROUTE_SUCCESS_MARKERS = (
    "检索完成",
    "检索成功",
    "已检索",
    "检索到",
    "查到",
    "搜到",
)


def _last_knowledge_search_result(requirement: Any) -> tuple[bool | None, int | None]:
    """最近一次 ``knowledge_search`` 能力调用的 ``(success, chunk_count)``。

    与 ``_last_result_ok`` 同口径：success 为 ``None`` 表示「没有可依据的
    检索结果」，与「检索失败」是两回事。knowledge_search 成功但零命中
    （chunk_count == 0）语义上等于「无法检索」，由调用方归入失败侧。
    """

    results = getattr(requirement, "prior_task_results", None) or []
    for item in reversed(list(results)):
        if not isinstance(item, dict):
            continue
        capability_results = item.get("capability_results")
        if not isinstance(capability_results, list) or not capability_results:
            continue
        for entry in reversed(capability_results):
            if not isinstance(entry, dict) or entry.get("tool_name") != "knowledge_search":
                continue
            if "success" not in entry:
                return None, None
            chunk_count = entry.get("chunk_count")
            if isinstance(chunk_count, bool) or not isinstance(chunk_count, int):
                chunk_count = None
            return bool(entry.get("success")), chunk_count
    return None, None


def _knowledge_route_next(
    edges: list[_EdgeCondition],
    search_success: bool | None,
    chunk_count: int | None,
) -> tuple[str, str, str]:
    """场景 C2：knowledge_query 节点按检索结果直判出边（零 LLM）。

    适用前提（任何一条不满足都交还 LLM）：
    - 本节点的知识预检索已完成（``search_success`` 非 None）；
    - 全部条件出边的条件文本**只以检索结果为自变量**——能被「检索完成」
      或「检索失败」词族归类；存在第三类语义条件（如「政策不满足」单独
      成边）时放弃，业务判断留给后续 decision 节点；
    - 恰好一条边与检索结果同向（无条件边让位给具体命中边，口径同场景 G）。

    返回 ``(next_node_id, outcome, miss_reason)``；不可判定返回
    ``("", "", reason)``，reason 供 ``knowledge_direct_route_skipped`` 观测：
    no_edges / no_search_result / unexplained_condition / no_unique_match。
    outcome ∈ retrieved（检索到内容）/ no_results（失败或零命中）。
    """

    if not edges:
        return "", "", "no_edges"
    if search_success is None:
        return "", "", "no_search_result"
    outcome_bad = (not search_success) or chunk_count == 0
    matched: list[str] = []
    for edge in edges:
        if edge.is_always:
            continue
        text = edge.condition_text
        if not text:
            # 既无条件文本又非 always——语义未知，保守交还 LLM
            return "", "", "unexplained_condition"
        if any(marker in text for marker in _KNOWLEDGE_ROUTE_FAILURE_MARKERS):
            hit = outcome_bad
        elif any(marker in text for marker in _KNOWLEDGE_ROUTE_SUCCESS_MARKERS):
            hit = not outcome_bad
        else:
            # 检索结果解释不了的条件（真正的业务判断）→ 交还 LLM
            return "", "", "unexplained_condition"
        if hit:
            matched.append(edge.target)
    if len(matched) == 1:
        return matched[0], ("no_results" if outcome_bad else "retrieved"), ""
    return "", "", "no_unique_match"


def _route_direct_next(edges: list[_EdgeCondition]) -> str:
    """场景 G：结构化条件求值后选唯一命中边；任何「未知」都交还 LLM。

    规则（保守优先）：
    - 一条边都没编译过 → 返回空串（交场景 D / LLM）；
    - 存在求值未知的**条件边**（未编译的自由文本、``llm_judge``、字段无从
      解析）→ 返回空串：无法排除它比命中边更该走；
    - 恰好一条条件边为真 → 它（``always`` 边视为兜底，此时让位）；
    - 没有条件边为真、且只有一条 ``always`` 边为真 → 走它（等价 if/else）；
    - 其余（多条为真 / 全都为假）→ 返回空串。
    """

    if not any(item.has_spec for item in edges):
        return ""
    specific_true = [item for item in edges if item.value is True and not item.is_always]
    unknown = [item for item in edges if item.value is None and not item.is_always]
    if unknown:
        return ""
    if len(specific_true) == 1:
        return specific_true[0].target
    if not specific_true:
        always_true = [item for item in edges if item.value is True]
        if len(always_true) == 1:
            return always_true[0].target
    return ""


def _unique_unconditional_next(edges: list[_EdgeCondition]) -> str:
    """唯一无条件出边时返回目标节点；有歧义或全是条件边则返回空串。"""

    unconditional: list[str] = []
    for edge in edges:
        if not edge.is_always:
            continue
        if edge.target not in unconditional:
            unconditional.append(edge.target)
    if len(unconditional) == 1:
        return unconditional[0]
    return ""


def _decision_direct_next(
    allowed_transitions: list[Any],
    known_slots: dict[str, Any],
    *,
    user_message: str = "",
    allowed_fields: set[str] | None = None,
) -> str:
    """场景 D：用槽位取值为分支条件找唯一证据边。

    规则（保守优先，任何歧义都交还 LLM）：
    - 只在**恰好一条**条件边命中时直判；0 条或多条命中返回空串。
    - 证据只来自已知槽位值（LLM 抽取过的规范化表达），不用用户原话，
      避免否定句（「不是管理员」）误配。
    - allowed_fields 非空时证据只取技能声明的字段（slot_fields）——
      会话历史遗留的脏槽位（如上一轮的 permission_preference）不参与判定。
    - 冲突否决：未被槽位命中的边，若其条件词元逐字出现在用户消息里
      （如用户明说「普通权限」），说明语义有歧义，交还 LLM。
    - 含否定语义（非/不/排除…）的边整体跳过，永远不由直判选出。
    - 匹配为双向包含（token⊆值 或 值⊆token），词元/取值至少 2 字。
    """

    if not isinstance(allowed_transitions, list) or not known_slots:
        return ""
    allowed = {str(item).strip() for item in (allowed_fields or set()) if str(item).strip()}
    values: list[str] = []
    for key, value in known_slots.items():
        if allowed and str(key).strip() not in allowed:
            # 非技能声明字段（历史遗留/LLM 自创槽位）不作为路由证据
            continue
        text = str(value).strip().lower()
        if len(text) >= _MIN_TOKEN_CHARS:
            values.append(text)
    if not values:
        return ""
    conditional: list[tuple[str, list[str]]] = []
    for edge in allowed_transitions:
        if not isinstance(edge, dict):
            continue
        target = str(edge.get("next_node_id") or "").strip()
        condition = str(edge.get("condition") or "").strip()
        if (
            not target
            or not condition
            or condition.lower() in _UNCONDITIONAL_EDGE_CONDITIONS
        ):
            continue
        if any(marker in condition for marker in _NEGATION_MARKERS):
            continue
        tokens = [
            token
            for token in (part.strip().lower() for part in _CONDITION_TOKEN_SPLIT.split(condition))
            if len(token) >= _MIN_TOKEN_CHARS
        ]
        if tokens:
            conditional.append((target, tokens))
    if not conditional:
        return ""
    matched: list[str] = []
    for target, tokens in conditional:
        if any(
            token in value or value in token
            for token in tokens
            for value in values
        ):
            if target not in matched:
                matched.append(target)
    if len(matched) != 1:
        return ""
    # 冲突否决：槽位证据命中了一条边，但用户消息里逐字出现了**另一条**边
    # 的条件词元（如槽位残留「生产环境」而用户明说「普通权限」）——
    # 语义冲突时不做直判，交还 LLM 结合上下文裁决。
    message = " ".join(str(user_message or "").split()).strip().lower()
    if message:
        matched_targets = set(matched)
        for target, tokens in conditional:
            if target in matched_targets:
                continue
            if any(len(token) >= _MIN_TOKEN_CHARS and token in message for token in tokens):
                return ""
    return matched[0]


