"""Laya 决策头接入（SOP 出边 + 知识路由）。

两个落点共用这一个类，各自一个实例、各自一套启用开关：

- :meth:`LayaRouter.route_edges` —— SOP 出边决策（场景 L）。确定性求值
  （``EdgeConditionSpec``，场景 G/D/C2）**判不了**时，用 Laya 做一次
  「选哪条出边」的 ``choice`` 判定，替代一整轮 ``harness.task_action`` 主模型
  决策；低置信 / 超时 / 非法答案一律回退，优先级固定为
  **确定性 spec > Laya > 主模型 LLM**。
- :meth:`LayaRouter.score_candidates` —— 知识路由（选文档 / 选桶）。Laya
  的 ``choice`` 一次只能选 1 个，满足不了 top-k，所以改成对每个候选问一个
  ``noul``「是否相关」，按相关概率排序取 top-k：**确定性路径（缓存 / 小候选
  短路 / 词法 fast path）> Laya > 现有 LLM 路由**。

设计约束（对应 .planning/quick/261008-jfj 的计划）：

- 纯编排 + 一层网络调用，不引入新的持久化状态；失败绝不抛穿主链路。
- ``state.background`` 只带判定必需的信息，不塞全量历史（Laya 是决策头，
  不是对话模型）。
- 每帧一个实例，连续失败达到阈值即熔断（本帧内不再调用），下一帧自然恢复。
- 影子模式（``shadow``）：照常计算并记录，但**绝不改变路由**，用于先看一致率。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from app.core.knowledge_flow.laya_client import LayaError, predict_raw
from app.core.sop.edge_eval import _EdgeCondition
from app.observability.spans import observed_span

QUESTION_KEY = "next_node"
_BACKGROUND_MAX_CHARS = 1600
_CRITERION_MAX_CHARS = 300
_VALUE_MAX_CHARS = 80
_PRIOR_RESULTS_MAX = 4

# 知识路由（P2'）：Laya 一次最多 30 个问题，与 ``app/api/laya.py`` 的
# ``MAX_QUESTIONS`` 契约一致。这里刻意不 import api 层，避免把 FastAPI
# 依赖带进 core。
MAX_QUESTIONS_PER_CALL = 30
_KNOWLEDGE_QUESTION_PREFIX = "cand"
_RELEVANCE_INSTRUCTION_MAX_CHARS = 2000
_RELEVANCE_LABEL_MAX_CHARS = 400


@dataclass
class LayaRouteDecision:
    """一次成功的出边判定。"""

    next_node_id: str
    confidence: float
    question_type: str
    latency_ms: float
    candidates: list[str] = field(default_factory=list)


@dataclass
class LayaRouteOutcome:
    """一次判定的完整结果：命中、或未命中/失败的原因。"""

    decision: LayaRouteDecision | None = None
    fallback_reason: str = ""
    error_kind: str = ""
    error: str = ""
    latency_ms: float = 0.0
    shadow: bool = False

    @property
    def routed(self) -> bool:
        return self.decision is not None

    def as_trace_payload(self) -> dict[str, Any]:
        """给会话时间线的可读载荷（不含 state 原文，避免泄漏业务内容）。"""

        decision = self.decision
        return {
            "outcome": (
                "shadow"
                if self.shadow and decision is not None
                else "routed"
                if decision is not None
                else "fallback"
            ),
            "reason": self.fallback_reason,
            "next_node_id": decision.next_node_id if decision else "",
            "confidence": decision.confidence if decision else None,
            "candidates": decision.candidates if decision else [],
            "latency_ms": self.latency_ms,
            "error_kind": self.error_kind,
        }


@dataclass
class LayaCandidateScore:
    """单个候选的相关概率（``noul`` = P(相关)）。"""

    key: str
    probability: float


@dataclass
class LayaKnowledgeOutcome:
    """一次知识路由判定的完整结果：逐候选分数、或未命中/失败的原因。

    ``scores`` 保持传入顺序（调用方排序时平局自然保留词法序）。
    """

    scores: list[LayaCandidateScore] = field(default_factory=list)
    fallback_reason: str = ""
    error_kind: str = ""
    latency_ms: float = 0.0
    candidate_count: int = 0
    calls: int = 0
    shadow: bool = False

    @property
    def scored(self) -> bool:
        return bool(self.scores)

    def as_trace_payload(self) -> dict[str, Any]:
        """给会话时间线的可读载荷（不含候选原文，避免泄漏业务内容）。"""

        return {
            "outcome": (
                "shadow"
                if self.shadow and self.scores
                else "routed"
                if self.scores
                else "fallback"
            ),
            "reason": self.fallback_reason,
            "candidate_count": self.candidate_count,
            "scored_count": len(self.scores),
            "calls": self.calls,
            "latency_ms": self.latency_ms,
            "error_kind": self.error_kind,
        }


def _short(value: Any, limit: int = _VALUE_MAX_CHARS) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _scoped_slots(requirement: Any, step: dict[str, Any]) -> dict[str, Any]:
    """只取技能声明的字段（与场景 G 的证据口径一致，隔离历史脏槽位）。"""

    slots = dict(getattr(requirement, "known_slots", {}) or {})
    sop = getattr(requirement, "sop_context", {}) or {}
    allowed = {
        str(item).strip()
        for item in (sop.get("slot_fields") or [])
        if str(item).strip()
    }
    if allowed:
        slots = {key: value for key, value in slots.items() if str(key) in allowed}
    return {key: value for key, value in slots.items() if value not in (None, "", [], {})}


def _prior_summary(requirement: Any) -> str:
    """最近几次能力/检索调用的一行摘要（工具名 + 成功与否）。"""

    results = getattr(requirement, "prior_task_results", None) or []
    lines: list[str] = []
    for item in reversed(list(results)):
        if not isinstance(item, Mapping):
            continue
        calls = item.get("capability_results")
        if not isinstance(calls, list):
            continue
        for entry in reversed(calls):
            if not isinstance(entry, Mapping):
                continue
            tool = str(entry.get("tool_name") or "").strip()
            if not tool:
                continue
            ok = entry.get("success")
            lines.append(f"{tool}={'成功' if ok else '失败' if ok is False else '未知'}")
            if len(lines) >= _PRIOR_RESULTS_MAX:
                return "；".join(reversed(lines))
    return "；".join(reversed(lines))


def build_background(requirement: Any, step: dict[str, Any]) -> str:
    """构造 Laya ``state.background``：只带判定必需的信息。"""

    sop = getattr(requirement, "sop_context", {}) or {}
    skill_name = str(sop.get("skill_name") or "").strip()
    step_name = str(step.get("name") or step.get("node_id") or step.get("step_id") or "").strip()
    instruction = str(step.get("instruction") or "").strip()

    header = f"SOP：{skill_name or '未命名'}"
    if step_name:
        header += f"；当前节点：{step_name}"
    if instruction:
        header += f"（{_short(instruction, 200)}）"

    lines = [header]

    slots = _scoped_slots(requirement, step)
    if slots:
        labels = sop.get("slot_labels") if isinstance(sop.get("slot_labels"), Mapping) else {}
        rendered = "；".join(
            f"{_short(labels.get(str(key), key), 40)}={_short(value, 60)}"
            for key, value in slots.items()
        )
        lines.append(f"已知信息：{rendered}")

    summary = _prior_summary(requirement)
    if summary:
        lines.append(f"已完成调用：{summary}")

    message = str(getattr(requirement, "source_user_message", "") or "").strip()
    if message:
        lines.append(f"用户最近消息：{_short(message, 300)}")

    return _short("\n".join(lines), _BACKGROUND_MAX_CHARS)


def build_criteria(
    edges: Sequence[_EdgeCondition],
    transitions: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """把出边压成 ``{next_node_id: 可读描述}``；``always`` 边标注为默认分支。"""

    names: dict[str, str] = {}
    for item in transitions:
        if not isinstance(item, Mapping):
            continue
        target = str(item.get("next_node_id") or "").strip()
        if target and target not in names:
            names[target] = str(item.get("next_node_name") or "").strip()

    criteria: dict[str, str] = {}
    for edge in edges:
        target = edge.target
        if not target or target in criteria:
            continue
        name = names.get(target) or target
        condition = edge.condition_text.strip()
        if edge.is_always or not condition:
            desc = f"默认分支：以上条件都不满足时进入「{name}」"
        else:
            desc = f"条件「{condition}」成立时进入「{name}」"
        criteria[target] = _short(desc, _CRITERION_MAX_CHARS)
    return criteria


def build_question(requirement: Any, edges: Sequence[_EdgeCondition], step: dict[str, Any]) -> dict[str, Any] | None:
    """构造一次 ``/predict`` 载荷；候选不足 2 个时返回 None（无需决策）。"""

    transitions = list(getattr(requirement, "allowed_transitions", []) or [])
    criteria = build_criteria(edges, transitions)
    if len(criteria) < 2:
        return None
    return {
        "state": {"background": build_background(requirement, step)},
        "questions": {
            QUESTION_KEY: {
                "type": "choice",
                "instructions": (
                    "根据背景判断当前 SOP 节点下一步应进入哪个节点，"
                    "只在给出的选项中选择最符合条件的一个。"
                ),
                "criteria": criteria,
            }
        },
    }


def build_relevance_background(query: str) -> str:
    """知识路由的 ``state.background``：只放用户问题（候选细节在各自问题里）。"""

    text = _short(query, _BACKGROUND_MAX_CHARS)
    if not text:
        return ""
    return f"用户问题：{text}"


def build_relevance_questions(
    candidates: Sequence[tuple[str, str]],
    *,
    key_prefix: str = _KNOWLEDGE_QUESTION_PREFIX,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """把候选压成 ``noul`` 问句；返回 ``(questions, key->候选id)``。

    key 用位置序号（``cand0``…），避免候选 id 里的特殊字符污染上游 key；
    再把 key 映射回真实 id。
    """

    questions: dict[str, dict[str, Any]] = {}
    key_to_id: dict[str, str] = {}
    for index, (candidate_id, label) in enumerate(candidates):
        key = f"{key_prefix}{index}"
        key_to_id[key] = str(candidate_id)
        questions[key] = {
            "type": "noul",
            "instructions": _short(
                f"候选内容「{_short(label, _RELEVANCE_LABEL_MAX_CHARS)}」"
                "是否与用户问题相关、需要被读取用于作答？",
                _RELEVANCE_INSTRUCTION_MAX_CHARS,
            ),
        }
    return questions, key_to_id


class LayaRouter:
    """每帧实例：负责构造问题、调用、阈值判断与熔断。"""

    def __init__(
        self,
        *,
        enabled: bool,
        shadow: bool = True,
        min_confidence: float = 0.7,
        timeout: float = 6.0,
        max_failures: int = 3,
        max_candidates: int = MAX_QUESTIONS_PER_CALL,
        questions_per_call: int = MAX_QUESTIONS_PER_CALL,
        predict: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.shadow = bool(shadow)
        self.min_confidence = float(min_confidence)
        self.timeout = float(timeout)
        self.max_failures = max(1, int(max_failures))
        self.max_candidates = max(1, int(max_candidates))
        self.questions_per_call = max(1, min(int(questions_per_call), MAX_QUESTIONS_PER_CALL))
        self._predict = predict or (lambda payload: predict_raw(payload, timeout=self.timeout))
        self._consecutive_failures = 0
        self._open = False

    # -- 供场景/测试观察 ---------------------------------------------------
    @property
    def open(self) -> bool:
        """熔断是否已触发（本帧内不再调用上游）。"""

        return self._open

    def route_edges(
        self,
        requirement: Any,
        edges: Sequence[_EdgeCondition],
        step: dict[str, Any],
    ) -> LayaRouteOutcome | None:
        """对当前节点的出边做一次判定。

        返回 ``None`` 表示「不适用」（未启用 / 候选不足）——调用方应继续原有流程。
        """

        if not self.enabled:
            return None
        if self._open:
            return LayaRouteOutcome(fallback_reason="circuit_open", shadow=self.shadow)

        payload = build_question(requirement, edges, step)
        if payload is None:
            return None
        criteria: dict[str, str] = payload["questions"][QUESTION_KEY]["criteria"]

        with observed_span(
            "laya_decision",
            "laya.edge_decision",
            question_type="choice",
            candidates=list(criteria.keys()),
            shadow=self.shadow,
        ) as span:
            try:
                body = self._predict(payload)
            except LayaError as exc:
                self._record_failure()
                span.finish(status="failed", error_kind=exc.kind, error=str(exc)[:300])
                return LayaRouteOutcome(
                    fallback_reason="error",
                    error_kind=exc.kind,
                    error=str(exc),
                    shadow=self.shadow,
                )
            except Exception as exc:  # noqa: BLE001 - 旁路绝不阻断主链路
                self._record_failure()
                span.finish(status="failed", error_kind="unexpected", error=str(exc)[:300])
                return LayaRouteOutcome(
                    fallback_reason="error",
                    error_kind="unexpected",
                    error=str(exc),
                    shadow=self.shadow,
                )

            self._reset_failures()
            outcome = self._interpret(body, criteria)
            span.finish(
                status="success",
                outcome=outcome.as_trace_payload()["outcome"],
                reason=outcome.fallback_reason,
                confidence=outcome.decision.confidence if outcome.decision else None,
                latency_ms=outcome.latency_ms,
            )
            return outcome

    # -- 知识路由（P2'）----------------------------------------------------
    def score_candidates(
        self,
        *,
        query: str,
        candidates: Sequence[tuple[str, str]],
        operation: str = "laya.knowledge_route",
        max_candidates: int | None = None,
        questions_per_call: int | None = None,
    ) -> LayaKnowledgeOutcome:
        """逐候选问 ``noul``「是否相关」，返回每条的相关概率。

        为什么不用 ``choice``：Laya 的 choice 一次只选 1 个，满足不了 top-k
        （文档 5 / 桶 4）——只能逐候选判定再排序。一次 ``/predict`` 最多
        :data:`MAX_QUESTIONS_PER_CALL` 问，超过则分批多次调用；候选数超过
        ``max_candidates`` 直接返回 ``oversize``，交调用方回退（多跳调用可能
        比一次 LLM 还慢）。
        """

        if not self.enabled:
            return LayaKnowledgeOutcome(fallback_reason="disabled", shadow=self.shadow)
        if self._open:
            return LayaKnowledgeOutcome(
                fallback_reason="circuit_open", shadow=self.shadow
            )

        limit = int(max_candidates if max_candidates is not None else self.max_candidates)
        per_call = int(
            questions_per_call
            if questions_per_call is not None
            else self.questions_per_call
        )
        per_call = max(1, min(per_call, MAX_QUESTIONS_PER_CALL))

        unique: list[tuple[str, str]] = []
        seen: set[str] = set()
        for candidate_id, label in candidates:
            key = str(candidate_id)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append((key, str(label)))
        if not unique:
            return LayaKnowledgeOutcome(
                fallback_reason="no_candidates", shadow=self.shadow
            )
        if len(unique) > limit:
            return LayaKnowledgeOutcome(
                fallback_reason="oversize",
                candidate_count=len(unique),
                shadow=self.shadow,
            )

        background = build_relevance_background(query)
        if not background:
            return LayaKnowledgeOutcome(
                fallback_reason="no_background", shadow=self.shadow
            )

        scores: list[LayaCandidateScore] = []
        latency_ms = 0.0
        calls = 0
        with observed_span(
            "laya_knowledge",
            operation,
            candidate_count=len(unique),
            shadow=self.shadow,
        ) as span:
            for start in range(0, len(unique), per_call):
                chunk = unique[start : start + per_call]
                questions, key_to_id = build_relevance_questions(chunk)
                payload = {"state": {"background": background}, "questions": questions}
                calls += 1
                try:
                    body = self._predict(payload)
                except LayaError as exc:
                    self._record_failure()
                    span.finish(status="failed", error_kind=exc.kind, error=str(exc)[:300])
                    return LayaKnowledgeOutcome(
                        fallback_reason="error",
                        error_kind=exc.kind,
                        candidate_count=len(unique),
                        calls=calls,
                        latency_ms=latency_ms,
                        shadow=self.shadow,
                    )
                except Exception as exc:  # noqa: BLE001 - 旁路绝不阻断主链路
                    self._record_failure()
                    span.finish(
                        status="failed", error_kind="unexpected", error=str(exc)[:300]
                    )
                    return LayaKnowledgeOutcome(
                        fallback_reason="error",
                        error_kind="unexpected",
                        candidate_count=len(unique),
                        calls=calls,
                        latency_ms=latency_ms,
                        shadow=self.shadow,
                    )

                self._reset_failures()
                latency_ms += _coerce_float(body.get("elapsed_ms")) or 0.0
                answers = body.get("answers") if isinstance(body, Mapping) else None
                if not isinstance(answers, Mapping):
                    span.finish(status="failed", error_kind="shape")
                    return LayaKnowledgeOutcome(
                        fallback_reason="missing_answers",
                        error_kind="shape",
                        candidate_count=len(unique),
                        calls=calls,
                        latency_ms=latency_ms,
                        shadow=self.shadow,
                    )
                for key, candidate_id in key_to_id.items():
                    answer = answers.get(key)
                    if not isinstance(answer, Mapping):
                        continue
                    probability = _coerce_float(answer.get("noul"))
                    if probability is None:
                        continue
                    scores.append(
                        LayaCandidateScore(key=candidate_id, probability=probability)
                    )

            span.finish(
                status="success",
                scored_count=len(scores),
                calls=calls,
                latency_ms=round(latency_ms, 3),
            )

        return LayaKnowledgeOutcome(
            scores=scores,
            candidate_count=len(unique),
            calls=calls,
            latency_ms=round(latency_ms, 3),
            shadow=self.shadow,
        )

    # -- 内部 -------------------------------------------------------------
    def _interpret(
        self,
        body: Mapping[str, Any],
        criteria: Mapping[str, str],
    ) -> LayaRouteOutcome:
        answers = body.get("answers")
        answer = answers.get(QUESTION_KEY) if isinstance(answers, Mapping) else None
        candidates = list(criteria.keys())
        latency_ms = _coerce_float(body.get("elapsed_ms")) or 0.0

        if not isinstance(answer, Mapping):
            return LayaRouteOutcome(
                fallback_reason="missing_answer", latency_ms=latency_ms, shadow=self.shadow
            )

        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in criteria:
            return LayaRouteOutcome(
                fallback_reason="invalid_choice",
                latency_ms=latency_ms,
                shadow=self.shadow,
            )

        confidence = _coerce_float(answer.get("confidence"))
        if confidence is None:
            return LayaRouteOutcome(
                fallback_reason="missing_confidence",
                latency_ms=latency_ms,
                shadow=self.shadow,
            )
        if confidence < self.min_confidence:
            return LayaRouteOutcome(
                fallback_reason="low_confidence",
                latency_ms=latency_ms,
                shadow=self.shadow,
            )

        return LayaRouteOutcome(
            decision=LayaRouteDecision(
                next_node_id=choice,
                confidence=confidence,
                question_type=str(answer.get("type") or "choice"),
                latency_ms=latency_ms,
                candidates=candidates,
            ),
            latency_ms=latency_ms,
            shadow=self.shadow,
        )

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.max_failures:
            self._open = True

    def _reset_failures(self) -> None:
        self._consecutive_failures = 0


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "LayaCandidateScore",
    "LayaKnowledgeOutcome",
    "LayaRouteDecision",
    "LayaRouteOutcome",
    "LayaRouter",
    "build_background",
    "build_criteria",
    "build_question",
    "build_relevance_background",
    "build_relevance_questions",
]