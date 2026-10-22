"""场景 L：Laya 出边决策旁路测试。

覆盖：
- 问题构造（choice criteria、默认分支标注、候选不足不下发）
- 阈值 / 非法答案 / 错误 / 熔断的降级
- 与现有场景表的协同：确定性可判时不打扰 Laya；影子模式保持原行为；
  关闭时与改造前一致
"""

from __future__ import annotations

from typing import Any

from app.core.laya_client import LayaError
from app.core.laya_router import LayaRouter, build_criteria, build_question
from app.core.sop_edge_eval import _EdgeCondition
from app.core.sop_step_executor import TRACE_EVENT, plan_sop_prefill_actions
from app.core.base.request_compiler import CapabilityManifest, TaskRequirement, _transitions


def _sop_requirement(**overrides: Any) -> TaskRequirement:
    step: dict[str, Any] = {
        "node_id": "n2",
        "name": "主管审批",
        "instruction": "判断是否放行",
        "expected_user_info": ["device_model", "urgency"],
    }
    base: dict[str, Any] = {
        "task_frame_id": "task-1",
        "kind": "sop",
        "goal": "完成 SOP 步骤",
        "source_user_message": "申请管理员权限，工号 3012",
        "sop_context": {
            "skill_id": "sop-1",
            "skill_name": "IT 服务",
            "step": step,
            "slot_fields": ["device_model", "urgency"],
        },
        "allowed_transitions": [
            {"next_node_id": "n9", "condition": "需要二级审批", "next_node_name": "二级审批"},
            {"next_node_id": "n3", "condition": "可直接开通", "next_node_name": "自动开通"},
        ],
        "capability_manifest": CapabilityManifest(),
    }
    base.update(overrides)
    return TaskRequirement(**base)


def _specs(transitions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from app.skills.edge_condition_spec import condition_fingerprint

    specs: dict[str, dict[str, Any]] = {}
    for edge in transitions:
        spec = edge.get("condition_spec")
        if not isinstance(spec, dict):
            continue
        fingerprint = condition_fingerprint(
            "n2", str(edge["next_node_id"]), edge.get("condition"), ["device_model", "urgency"]
        )
        specs[fingerprint] = spec
    return specs


# 两条都编译成 llm_judge → 场景 G 无法唯一判定 → 正是 Laya 该介入的场景
_AMBIGUOUS_EDGES: list[dict[str, Any]] = [
    {
        "next_node_id": "n9",
        "condition": "需要二级审批",
        "next_node_name": "二级审批",
        "condition_spec": {"kind": "llm_judge"},
    },
    {
        "next_node_id": "n3",
        "condition": "可直接开通",
        "next_node_name": "自动开通",
        "condition_spec": {"kind": "llm_judge"},
    },
]


def _ambiguous_edges() -> list[_EdgeCondition]:
    from app.skills.edge_condition_spec import spec_from_payload

    return [
        _EdgeCondition(
            target=item["next_node_id"],
            condition_text=item["condition"],
            spec=spec_from_payload(item["condition_spec"]),
            value=None,
        )
        for item in _AMBIGUOUS_EDGES
    ]


def _predict_returning(
    choice: str, confidence: float = 0.95, elapsed_ms: float = 11.5
):
    def predict(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "answers": {
                "next_node": {"type": "choice", "choice": choice, "confidence": confidence}
            },
            "elapsed_ms": elapsed_ms,
        }

    return predict


def _router(choice: str = "n9", confidence: float = 0.95, *, shadow: bool = False, **kwargs: Any) -> LayaRouter:
    return LayaRouter(
        enabled=True,
        shadow=shadow,
        min_confidence=0.7,
        predict=_predict_returning(choice, confidence),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 问题构造
# ---------------------------------------------------------------------------


def test_build_criteria_labels_conditions_and_default_branch() -> None:
    from app.skills.edge_condition_spec import always_spec, spec_from_payload

    edges = [
        _EdgeCondition(
            target="n9",
            condition_text="需要二级审批",
            spec=spec_from_payload({"kind": "llm_judge"}),
            value=None,
        ),
        _EdgeCondition(target="n3", condition_text="", spec=always_spec(), value=True),
    ]
    criteria = build_criteria(
        edges,
        [
            {"next_node_id": "n9", "next_node_name": "二级审批"},
            {"next_node_id": "n3", "next_node_name": "自动开通"},
        ],
    )
    assert set(criteria) == {"n9", "n3"}
    assert "需要二级审批" in criteria["n9"] and "二级审批" in criteria["n9"]
    assert "默认分支" in criteria["n3"]


def test_build_question_requires_two_candidates() -> None:
    from app.skills.edge_condition_spec import spec_from_payload

    one_edge = [
        _EdgeCondition(
            target="n9",
            condition_text="需要二级审批",
            spec=spec_from_payload({"kind": "llm_judge"}),
            value=None,
        )
    ]
    assert build_question(_sop_requirement(), one_edge, {"node_id": "n2", "name": "审批"}) is None


def test_build_question_payload_shape() -> None:
    payload = build_question(
        _sop_requirement(allowed_transitions=_AMBIGUOUS_EDGES),
        _ambiguous_edges(),
        {"node_id": "n2", "name": "主管审批"},
    )
    assert payload is not None
    question = payload["questions"]["next_node"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"n9", "n3"}
    background = payload["state"]["background"]
    assert background.startswith("SOP：IT 服务")
    assert "主管审批" in background
    assert "用户最近消息" in background


# ---------------------------------------------------------------------------
# 阈值 / 非法答案 / 错误 / 熔断
# ---------------------------------------------------------------------------


def test_router_routes_on_confident_choice() -> None:
    router = _router(choice="n9", confidence=0.95)
    outcome = router.route_edges(
        _sop_requirement(allowed_transitions=_AMBIGUOUS_EDGES),
        _ambiguous_edges(),
        {"node_id": "n2"},
    )
    assert outcome is not None and outcome.decision is not None
    assert outcome.decision.next_node_id == "n9"
    assert outcome.decision.confidence == 0.95


def test_router_low_confidence_falls_back() -> None:
    router = _router(choice="n9", confidence=0.4)
    outcome = router.route_edges(
        _sop_requirement(allowed_transitions=_AMBIGUOUS_EDGES),
        _ambiguous_edges(),
        {"node_id": "n2"},
    )
    assert outcome is not None and outcome.decision is None
    assert outcome.fallback_reason == "low_confidence"


def test_router_invalid_choice_falls_back() -> None:
    router = _router(choice="nope", confidence=0.99)
    outcome = router.route_edges(
        _sop_requirement(allowed_transitions=_AMBIGUOUS_EDGES),
        _ambiguous_edges(),
        {"node_id": "n2"},
    )
    assert outcome is not None and outcome.decision is None
    assert outcome.fallback_reason == "invalid_choice"


def test_router_error_opens_circuit_breaker() -> None:
    def _boom(_payload: dict[str, Any]) -> dict[str, Any]:
        raise LayaError("connect refused", kind="network")

    router = LayaRouter(enabled=True, max_failures=3, predict=_boom)
    request = _sop_requirement(allowed_transitions=_AMBIGUOUS_EDGES)
    edges = _ambiguous_edges()
    for _ in range(3):
        outcome = router.route_edges(request, edges, {"node_id": "n2"})
        assert outcome is not None and outcome.fallback_reason == "error"
    assert router.open is True
    opened = router.route_edges(request, edges, {"node_id": "n2"})
    assert opened is not None and opened.fallback_reason == "circuit_open"


# ---------------------------------------------------------------------------
# 场景表协同
# ---------------------------------------------------------------------------


def test_scene_l_routes_ambiguous_edge() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    router = _router(choice="n9", shadow=False, confidence=0.95)
    actions = plan_sop_prefill_actions(
        _sop_requirement(allowed_transitions=_AMBIGUOUS_EDGES),
        edge_condition_specs=_specs(_AMBIGUOUS_EDGES),
        laya_router=router,
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "n9"
    payload = next(
        p for t, p in events if t == TRACE_EVENT and p.get("scene") == "laya_edge_decision"
    )
    assert payload["outcome"] == "routed"
    assert payload["next_node_id"] == "n9"


def test_scene_l_shadow_keeps_original_behavior() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    router = _router(choice="n9", shadow=True, confidence=0.95)
    actions = plan_sop_prefill_actions(
        _sop_requirement(allowed_transitions=_AMBIGUOUS_EDGES),
        edge_condition_specs=_specs(_AMBIGUOUS_EDGES),
        laya_router=router,
        trace_sink=lambda t, p: events.append((t, p)),
    )
    # 影子模式不改路由：G 判不了 → 返回空，交还 LLM
    assert actions == []
    payload = next(
        p for t, p in events if t == TRACE_EVENT and p.get("scene") == "laya_edge_decision"
    )
    assert payload["outcome"] == "shadow"
    assert payload["next_node_id"] == "n9"


def test_scene_l_low_confidence_falls_through() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    router = _router(choice="n9", shadow=False, confidence=0.3)
    actions = plan_sop_prefill_actions(
        _sop_requirement(allowed_transitions=_AMBIGUOUS_EDGES),
        edge_condition_specs=_specs(_AMBIGUOUS_EDGES),
        laya_router=router,
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert actions == []
    payload = next(
        p for t, p in events if t == TRACE_EVENT and p.get("scene") == "laya_edge_decision"
    )
    assert payload["outcome"] == "fallback"
    assert payload["reason"] == "low_confidence"


def test_scene_l_not_called_when_spec_can_decide() -> None:
    def _boom(_payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("确定性可判时不应调用 Laya")

    transitions = [
        {
            "next_node_id": "n9",
            "condition": "所有必填信息都收集完成后进入",
            "next_node_name": "二级审批",
            "condition_spec": {"kind": "slots_all", "fields": ["device_model", "urgency"]},
        },
        {
            "next_node_id": "n3",
            "condition": "",
            "next_node_name": "自动开通",
            "condition_spec": {"kind": "always"},
        },
    ]
    router = LayaRouter(enabled=True, shadow=False, min_confidence=0.7, predict=_boom)
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=transitions,
            known_slots={"device_model": "X1", "urgency": "高"},
        ),
        edge_condition_specs=_specs(transitions),
        laya_router=router,
    )
    assert [action["next_step_id"] for action in actions] == ["n9"]


def test_scene_l_disabled_is_byte_identical() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    with_router_off = plan_sop_prefill_actions(
        _sop_requirement(allowed_transitions=_AMBIGUOUS_EDGES),
        edge_condition_specs=_specs(_AMBIGUOUS_EDGES),
        laya_router=None,
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert with_router_off == []
    assert all(p.get("scene") != "laya_edge_decision" for t, p in events if t == TRACE_EVENT)


def test_router_disabled_returns_none() -> None:
    router = LayaRouter(enabled=False)
    assert router.route_edges(_sop_requirement(), [], {}) is None


# ---------------------------------------------------------------------------
# 编译期：出边补目标节点名
# ---------------------------------------------------------------------------


def test_transitions_include_target_node_name() -> None:
    from app.db.models import Skill

    skill = Skill(
        tenant_id="t",
        skill_id="s",
        name="S",
        content_json={
            "nodes": [
                {"node_id": "n2", "name": "主管审批"},
                {"node_id": "n9", "name": "二级审批"},
            ],
            "edges": [{"source_node_id": "n2", "next_node_id": "n9", "condition": "需要二级"}],
        },
    )
    transitions = _transitions(skill, {"node_id": "n2"})
    assert transitions[0]["next_node_id"] == "n9"
    assert transitions[0]["next_node_name"] == "二级审批"