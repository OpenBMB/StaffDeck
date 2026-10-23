"""SOP 确定性执行器（P1）测试。

覆盖三个确定性场景（预检索/纯流转/缺槽询问）的触发与降级边界，
以及挂载到 HarnessTaskAgent 后「纯流转节点零 LLM」的集成行为。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.engine.step_loop import HarnessTaskAgent
from app.core.sop.step_executor import TRACE_EVENT, plan_sop_prefill_actions
from app.core.base.request_compiler import (
    CapabilityManifest,
    TaskRequirement,
)


def _sop_requirement(**overrides: Any) -> TaskRequirement:
    step: dict[str, Any] = {
        "node_id": "n2",
        "name": "提交申请",
        "instruction": "提交申请单",
        "expected_user_info": ["device_model", "urgency"],
    }
    if "expected_user_info" in overrides:
        step["expected_user_info"] = overrides.pop("expected_user_info")
    base: dict[str, Any] = {
        "task_frame_id": "task-1",
        "kind": "sop",
        "goal": "完成 SOP 步骤",
        "source_user_message": "帮我申领一台电脑",
        "sop_context": {
            "skill_id": "sop-1",
            "skill_name": "IT 服务",
            "step": step,
        },
        "allowed_transitions": [{"next_node_id": "n3", "condition": ""}],
        "capability_manifest": CapabilityManifest(),
    }
    base.update(overrides)
    return TaskRequirement(**base)


def _events() -> list[tuple[str, dict[str, Any]]]:
    return []


# ---------------------------------------------------------------------------
# 场景 C：预检索
# ---------------------------------------------------------------------------


def test_prefetch_knowledge_when_required_kb_and_no_slot_gap() -> None:
    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_knowledge_base_ids=["kb-it"],
            required_slots=[],
        ),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "tool"
    assert action["tool_name"] == "knowledge_search"
    assert action["arguments"]["query"] == "帮我申领一台电脑"
    assert action["arguments"]["knowledge_base_ids"] == ["kb-it"]
    assert events and events[0][0] == TRACE_EVENT
    assert events[0][1]["scene"] == "prefetch_knowledge"


def test_prefetch_skipped_when_kb_already_satisfied_in_checkpoint() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_knowledge_base_ids=["kb-it"]),
        satisfied_required_knowledge_ids={"kb-it"},
    )
    assert actions == []


def test_prefetch_skipped_when_slot_gap_exists() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_knowledge_base_ids=["kb-it"],
            required_slots=["device_model"],
        ),
    )
    assert actions == []


def test_prefetch_skipped_when_query_empty() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_knowledge_base_ids=["kb-it"], source_user_message="  "),
    )
    assert actions == []


# ---------------------------------------------------------------------------
# 场景 C2：knowledge_query 节点检索结果直判（零 LLM）
# ---------------------------------------------------------------------------

_POLICY_EDGES = [
    {"next_node_id": "query_balance", "condition": "政策检索完成"},
    {"next_node_id": "handoff_hr", "condition": "政策不满足或无法检索"},
]


def _requirement_with_search_result(
    success: bool | None,
    chunk_count: int | None = None,
) -> TaskRequirement:
    prior: list[dict[str, Any]] = []
    if success is not None:
        entry: dict[str, Any] = {"tool_name": "knowledge_search", "success": success}
        if chunk_count is not None:
            entry["chunk_count"] = chunk_count
        prior = [{"capability_results": [entry]}]
    return _sop_requirement(
        required_knowledge_base_ids=["kb-policy"],
        allowed_transitions=_POLICY_EDGES,
        prior_task_results=prior,
    )


def test_knowledge_direct_route_on_retrieved() -> None:
    """检索到内容 → 走「检索完成」边（政策判断留给后续 decision 节点）。"""

    events = _events()
    actions = plan_sop_prefill_actions(
        _requirement_with_search_result(success=True, chunk_count=3),
        satisfied_required_knowledge_ids={"kb-policy"},
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "completed"
    assert action["next_step_id"] == "query_balance"
    assert events[0][1]["scene"] == "knowledge_direct_route"
    assert events[0][1]["outcome"] == "retrieved"


def test_knowledge_direct_route_on_zero_chunks_goes_failure_edge() -> None:
    """检索成功但零命中 = 「无法检索」→ 走失败边。"""

    actions = plan_sop_prefill_actions(
        _requirement_with_search_result(success=True, chunk_count=0),
        satisfied_required_knowledge_ids={"kb-policy"},
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "handoff_hr"


def test_knowledge_direct_route_on_search_error_goes_failure_edge() -> None:
    actions = plan_sop_prefill_actions(
        _requirement_with_search_result(success=False, chunk_count=0),
        satisfied_required_knowledge_ids={"kb-policy"},
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "handoff_hr"


def test_knowledge_direct_route_skipped_without_search_result() -> None:
    """没有可依据的检索结果（None ≠ False）→ 交还 LLM，带观测事件。"""

    events = _events()
    actions = plan_sop_prefill_actions(
        _requirement_with_search_result(success=None),
        satisfied_required_knowledge_ids={"kb-policy"},
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert actions == []
    assert events[0][1]["scene"] == "knowledge_direct_route_skipped"
    assert events[0][1]["reason"] == "no_search_result"


def test_knowledge_direct_route_skipped_on_semantic_condition() -> None:
    """存在检索结果解释不了的语义条件边（如「政策不满足」单独成边）→ 交还 LLM。"""

    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_knowledge_base_ids=["kb-policy"],
            allowed_transitions=[
                {"next_node_id": "query_balance", "condition": "政策检索完成"},
                {"next_node_id": "handoff_hr", "condition": "政策不满足"},
            ],
            prior_task_results=[
                {
                    "capability_results": [
                        {
                            "tool_name": "knowledge_search",
                            "success": True,
                            "chunk_count": 3,
                        }
                    ]
                }
            ],
        ),
        satisfied_required_knowledge_ids={"kb-policy"},
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert actions == []
    assert events[0][1]["scene"] == "knowledge_direct_route_skipped"
    assert events[0][1]["reason"] == "unexplained_condition"


def test_knowledge_direct_route_ignores_read_file_and_stale_results() -> None:
    """同一帧里 search 之后还调了 read_file；且更早帧的 search 不算数。"""

    # 同帧有 read_file（非 search）→ 仍应识别到 search 的结果
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_knowledge_base_ids=["kb-policy"],
            allowed_transitions=_POLICY_EDGES,
            prior_task_results=[
                {
                    "capability_results": [
                        {"tool_name": "knowledge_search", "success": True, "chunk_count": 2},
                        {"tool_name": "read_file", "success": True},
                    ]
                }
            ],
        ),
        satisfied_required_knowledge_ids={"kb-policy"},
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "query_balance"

    # search 只出现在非最近帧但全库只有这一次 search —— 最近帧无 search 也应取到
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_knowledge_base_ids=["kb-policy"],
            allowed_transitions=_POLICY_EDGES,
            prior_task_results=[
                {
                    "capability_results": [
                        {"tool_name": "knowledge_search", "success": False}
                    ]
                },
                {"capability_results": [{"tool_name": "read_file", "success": True}]},
            ],
        ),
        satisfied_required_knowledge_ids={"kb-policy"},
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "handoff_hr"


# ---------------------------------------------------------------------------
# 场景 B：纯流转直通
# ---------------------------------------------------------------------------


def test_passthrough_transition_on_unique_unconditional_edge() -> None:
    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "completed"
    assert action["next_step_id"] == "n3"
    assert action["reply_fragment"] == ""
    assert events[0][1]["scene"] == "passthrough_transition"


def test_passthrough_skipped_on_multiple_unconditional_edges() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=[
                {"next_node_id": "n3", "condition": ""},
                {"next_node_id": "n4", "condition": "default"},
            ]
        ),
    )
    assert actions == []


def test_passthrough_skipped_when_only_conditional_edges() -> None:
    """condition 是自由文本，无法静态求值——一律降级。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=[{"next_node_id": "n3", "condition": "需要外部商品数据"}]
        ),
    )
    assert actions == []


def test_passthrough_skipped_when_no_transitions() -> None:
    actions = plan_sop_prefill_actions(_sop_requirement(allowed_transitions=[]))
    assert actions == []


# ---------------------------------------------------------------------------
# 场景 A：缺槽直通
# ---------------------------------------------------------------------------


def test_await_user_on_missing_slots_without_required_capabilities() -> None:
    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model", "urgency"]),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "awaiting_user"
    # 字段名在回复里必须是中文显示名：这轮是零 LLM 模板回复，
    # 直接把 device_model 抛给用户等于没说话
    assert "设备型号" in action["reply_fragment"]
    assert "紧急程度" in action["reply_fragment"]
    assert "device_model" not in action["reply_fragment"]
    assert "提交申请" in action["reply_fragment"]
    assert events[0][1]["scene"] == "await_user_for_slots"


def test_await_user_uses_required_slots_when_expected_info_empty() -> None:
    requirement = _sop_requirement(required_slots=["device_model"])
    requirement.sop_context["step"]["expected_user_info"] = []
    actions = plan_sop_prefill_actions(requirement)
    assert len(actions) == 1
    assert "设备型号" in actions[0]["reply_fragment"]
    assert "device_model" not in actions[0]["reply_fragment"]


def test_await_user_honours_skill_declared_slot_labels() -> None:
    """技能自己声明的 slot_labels 优先于内置词典。"""

    requirement = _sop_requirement(required_slots=["employee_id", "device_model"])
    requirement.sop_context["slot_labels"] = {"employee_id": "工号（HR 系统）"}
    actions = plan_sop_prefill_actions(requirement)
    assert len(actions) == 1
    reply = actions[0]["reply_fragment"]
    assert "工号（HR 系统）" in reply
    # 未声明的字段回落到内置词典
    assert "设备型号" in reply


def test_await_user_keeps_unknown_field_identifier() -> None:
    """词典没有、技能也没声明的字段：宁可显示标识符，也不猜一个错的中文名。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["some_vendor_specific_code"]),
    )
    assert len(actions) == 1
    assert "some_vendor_specific_code" in actions[0]["reply_fragment"]


def test_await_user_skipped_when_required_capabilities_present() -> None:
    """节点有强制能力时不能直接问用户——可能先调工具就能补齐信息。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["device_model"],
            required_capability_names=["it_asset_tool"],
        ),
    )
    assert actions == []


# ---------------------------------------------------------------------------
# 降级边界
# ---------------------------------------------------------------------------


def test_conversation_kind_never_prefills() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(kind="conversation"),
    )
    assert actions == []


def test_unresolvable_step_degrades() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(sop_context={"skill_id": "sop-1"}),
    )
    assert actions == []


def test_same_step_resume_degrades() -> None:
    """断点恢复到同一步骤时 transcript 已有中间状态，不介入。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model"]),
        same_step=True,
    )
    assert actions == []


# ---------------------------------------------------------------------------
# 恢复补槽会话（same_step + 上次终点 awaiting_user）放行场景 A
# ---------------------------------------------------------------------------


def test_resume_awaiting_user_extraction_completes_then_passthrough(
    monkeypatch,
) -> None:
    """用户回来补槽位（如 15:28 复测场景）：抽齐 → 直通流转，不再 7 轮 LLM。"""
    import app.core.sop.scenes as scenes_module

    monkeypatch.setattr(
        scenes_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: {
            "employee_id": "3012",
            "system": "OA系统",
            "permission": "管理员",
            "access_level": "生产环境管理员",
        },
    )
    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system", "permission", "access_level"],
            expected_user_info=["employee_id", "system", "permission", "access_level"],
        ),
        same_step=True,
        resumed_awaiting_user=True,
        slot_extraction_model="fake-model",
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "completed"
    assert action["slot_updates"]["employee_id"] == "3012"
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["passthrough_transition_after_extraction"]


def test_resume_awaiting_user_partial_extraction_asks_missing_only(
    monkeypatch,
) -> None:
    """恢复补槽但只抽到部分：只问缺的，已抽值落库防重复问。"""
    import app.core.sop.scenes as scenes_module

    monkeypatch.setattr(
        scenes_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: {"employee_id": "3012"},
    )
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system", "permission", "access_level"],
            expected_user_info=["employee_id", "system", "permission", "access_level"],
        ),
        same_step=True,
        resumed_awaiting_user=True,
        slot_extraction_model="fake-model",
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["status"] == "awaiting_user"
    assert "目标系统" in action["reply_fragment"]
    assert "员工工号" not in action["reply_fragment"]
    assert action["slot_updates"] == {"employee_id": "3012"}


def test_resume_awaiting_user_blocks_prefetch_but_allows_passthrough() -> None:
    """恢复补槽会话不重复预检索（C 排除），但槽位齐时放行流转直通（B）。

    2026-09-15 死胡同修复：旧实现里恢复帧槽位已齐会静默 return []，
    整帧白白落一轮 LLM；现在 F/D/G/B 与普通帧同表调度。
    """
    # 场景 C：kb 必填且无槽位缺口 → 恢复场景下不介入（E 门控接住后放弃）
    assert (
        plan_sop_prefill_actions(
            _sop_requirement(required_knowledge_base_ids=["kb-it"]),
            same_step=True,
            resumed_awaiting_user=True,
        )
        == []
    )
    # 场景 B：无槽位无 kb 的纯流转 → 恢复场景下照常直通（零 LLM）
    actions = plan_sop_prefill_actions(
        _sop_requirement(),
        same_step=True,
        resumed_awaiting_user=True,
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "completed"
    assert actions[0]["next_step_id"] == "n3"


def test_resume_awaiting_user_with_kb_slots_step_degrades() -> None:
    """恢复补槽 + 步骤同时要求 kb：抽取结果无法安全推进，退回现有链路。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["device_model"],
            required_knowledge_base_ids=["kb-it"],
        ),
        same_step=True,
        resumed_awaiting_user=True,
    )
    assert actions == []


def test_malformed_requirement_degrades_silently() -> None:
    class Broken:
        kind = "sop"

        @property
        def sop_context(self) -> Any:
            raise RuntimeError("boom")

    assert plan_sop_prefill_actions(Broken()) == []
    assert plan_sop_prefill_actions(None) == []


# ---------------------------------------------------------------------------
# 集成：挂载后纯流转节点零 LLM 完成
# ---------------------------------------------------------------------------


def test_harness_passthrough_node_completes_without_llm(monkeypatch) -> None:
    from app.core.engine import step_loop as harness_agent_module
    from app.db.models import ModelConfig

    def _fail_llm(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("纯流转节点不应触发任何 LLM 调用")

    monkeypatch.setattr(harness_agent_module, "LLMClient", _fail_llm)

    trace_events: list[tuple[str, dict[str, Any]]] = []

    def _invoke_tool(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("纯流转节点不应调用任何工具")

    result = HarnessTaskAgent().run(
        _sop_requirement(),
        ModelConfig(
            tenant_id="t", name="m", provider="openai", base_url="http://x/v1",
            api_key_encrypted="x", model_name="gpt", purpose="chat",
        ),
        _invoke_tool,
        max_actions=3,
        trace_sink=lambda t, p: trace_events.append((t, p)),
    )

    assert result.status == "completed"
    assert result.next_step_id == "n3"
    assert result.action_count == 1
    planned = next(
        payload for event_type, payload in trace_events if event_type == TRACE_EVENT
    )
    assert planned["scene"] == "passthrough_transition"


# ---------------------------------------------------------------------------
# 集成：预检索动作真实执行并满足 required_knowledge_base_ids
# ---------------------------------------------------------------------------


def test_harness_prefetch_executes_and_satisfies_required_knowledge(monkeypatch) -> None:
    from app.core.engine import step_loop as harness_agent_module
    from app.core.base.request_compiler import CapabilityDescriptor
    from app.db.models import ModelConfig

    llm_calls: list[dict[str, Any]] = []

    class FakeLLMClient:
        def __init__(self, _model_config: ModelConfig) -> None:
            pass

        def generate_json(
            self, system_prompt: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            llm_calls.append(payload)
            return {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "已根据知识库回答。",
                "task_summary": "完成。",
            }

    monkeypatch.setattr(harness_agent_module, "LLMClient", FakeLLMClient)

    trace_events: list[tuple[str, dict[str, Any]]] = []

    def _invoke_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "knowledge_search"
        return {
            "success": True,
            "data": {"evidence_pack": [{"content": "申领流程：..."}]},
            "citations": [{"source": "kb-it"}],
        }

    result = HarnessTaskAgent().run(
        _sop_requirement(
            required_knowledge_base_ids=["kb-it"],
            required_capability_names=["knowledge_search"],
            capability_manifest=CapabilityManifest(
                available=[
                    CapabilityDescriptor(
                        capability_id="ks",
                        name="knowledge_search",
                        kind="internal",
                    )
                ]
            ),
        ),
        ModelConfig(
            tenant_id="t", name="m", provider="openai", base_url="http://x/v1",
            api_key_encrypted="x", model_name="gpt", purpose="chat",
        ),
        _invoke_tool,
        max_actions=3,
        trace_sink=lambda t, p: trace_events.append((t, p)),
    )

    # 预检索执行后 LLM 首轮直接面对证据收尾：仅 1 次 LLM 调用
    assert result.status == "completed"
    assert result.reply_fragment == "已根据知识库回答。"
    assert len(llm_calls) == 1
    planned = next(
        payload for event_type, payload in trace_events if event_type == TRACE_EVENT
    )
    assert planned["scene"] == "prefetch_knowledge"


# ---------------------------------------------------------------------------
# P2：场景 A 前置轻量槽位抽取
# ---------------------------------------------------------------------------


def test_slot_extraction_completes_slots_then_passthrough(monkeypatch) -> None:
    """复现截图 bug 场景：用户首条消息已带齐信息 → 抽齐后直通，不再错误询问。"""
    import app.core.sop.scenes as scenes_module

    def fake_extract(*args, **kwargs):
        assert "employee_id" in args[0] or "employee_id" in kwargs.get("fields", [])
        assert kwargs.get("step_instruction") == "" or "step_instruction" in kwargs
        return {
            "employee_id": "3012",
            "system": "OA系统",
            "permission": "管理员",
            "access_level": "生产环境管理员",
        }

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fake_extract)

    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system", "permission", "access_level"],
            expected_user_info=["employee_id", "system", "permission", "access_level"],
            known_slots={},
        ),
        slot_extraction_model="fake-model",
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "completed"
    assert action["next_step_id"] == "n3"
    assert action["slot_updates"]["employee_id"] == "3012"
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["passthrough_transition_after_extraction"]


def test_slot_extraction_partial_asks_missing_only(monkeypatch) -> None:
    """部分抽取：只问缺的字段，已抽值随 slot_updates 落库防重复问。"""
    import app.core.sop.scenes as scenes_module

    monkeypatch.setattr(
        scenes_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: {"employee_id": "3012"},
    )

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system", "permission", "access_level"],
            expected_user_info=["employee_id", "system", "permission", "access_level"],
        ),
        slot_extraction_model="fake-model",
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["status"] == "awaiting_user"
    assert "目标系统" in action["reply_fragment"]
    assert "访问级别" in action["reply_fragment"]
    assert "员工工号" not in action["reply_fragment"]
    assert action["slot_updates"] == {"employee_id": "3012"}


def test_slot_extraction_failure_falls_back_to_template(monkeypatch) -> None:
    """抽取返回空结果退回模板直通（_extract_slots_llm 内部兜异常返回 {}）。"""
    import app.core.sop.scenes as scenes_module

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", lambda *args, **kwargs: {})
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model"]),
        slot_extraction_model="fake-model",
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "awaiting_user"
    assert actions[0]["slot_updates"] == {}


def test_unexpected_executor_error_degrades_to_empty(monkeypatch) -> None:
    """执行器实现层异常：主流程整体返回空 → harness 走现有 LLM 链路。"""
    import app.core.sop.scenes as scenes_module

    def boom(*args):
        raise RuntimeError("bug")

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", boom)
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model"]),
        slot_extraction_model="fake-model",
    )
    assert actions == []


def test_slot_extraction_filters_unknown_fields(monkeypatch) -> None:
    """抽取返回的未知字段被过滤，不进入 slot_updates。"""
    import app.core.sop.scenes as scenes_module

    monkeypatch.setattr(
        scenes_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: {"device_model": "ThinkPad", "hacker_field": "x"},
    )
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model", "urgency"]),
        slot_extraction_model="fake-model",
    )
    action = actions[0]
    assert action["status"] == "awaiting_user"
    assert action["slot_updates"] == {"device_model": "ThinkPad"}


def test_slot_extraction_skipped_without_model() -> None:
    """未传模型（如单测环境）：跳过抽取，行为与 P1 模板直通一致。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model"]),
    )
    assert actions[0]["status"] == "awaiting_user"


def test_slot_extraction_receives_step_instruction(monkeypatch) -> None:
    """节点说明作为 field_context 传给抽取调用，供模型理解抽象字段语义。"""
    import app.core.sop.scenes as scenes_module

    captured: dict[str, Any] = {}

    def fake_extract(*args, **kwargs):
        captured["fields"] = args[0]
        captured["step_instruction"] = kwargs.get("step_instruction")
        captured["model_config"] = args[3]
        return {"employee_id": "1002", "system": "crm"}

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fake_extract)
    requirement = _sop_requirement(
        required_slots=["employee_id", "system", "permission", "access_level"],
        expected_user_info=["employee_id", "system", "permission", "access_level"],
    )
    requirement.sop_context["step"]["instruction"] = (
        "收集员工工号、目标系统、权限级别及访问级别/环境类型。"
    )
    plan_sop_prefill_actions(
        requirement,
        slot_extraction_model="fake-model",
    )
    assert captured["step_instruction"] == (
        "收集员工工号、目标系统、权限级别及访问级别/环境类型。"
    )
    assert captured["model_config"] == "fake-model"


# ---------------------------------------------------------------------------
# P3 场景 D：decision 节点分支条件槽位直判
# ---------------------------------------------------------------------------

_DECISION_STEP = {
    "node_id": "n2",
    "type": "decision",
    "name": "权限级别分流判断",
    "instruction": "根据 access_level/permission 判断是否高权限。",
}

_ROUTE_EDGES = [
    {"next_node_id": "n3", "condition": "普通权限"},
    {"next_node_id": "n6", "condition": "高权限/生产环境/敏感数据"},
]


def test_decision_direct_eval_unique_match_transitions(monkeypatch) -> None:
    """复现 16:0x 场景：access_level=生产环境 唯一命中高权限边 → 零 LLM 直判。"""
    import app.core.sop.scenes as scenes_module

    def fail_llm(*args, **kwargs):
        raise AssertionError("直判命中不应触发槽位抽取/LLM")

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fail_llm)

    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "skill_name": "权限开通工单分流", "step": _DECISION_STEP},
            allowed_transitions=_ROUTE_EDGES,
            known_slots={
                "employee_id": "1002",
                "system": "crm",
                "permission": "管理员",
                "access_level": "生产环境",
            },
        ),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "completed"
    assert action["next_step_id"] == "n6"
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["decision_direct_eval"]


def test_decision_direct_eval_ambiguous_falls_back() -> None:
    """泛化取值同时命中两条边（「权限」⊂ 普通权限/高权限）→ 降级 LLM。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _DECISION_STEP},
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"permission": "权限"},
        ),
    )
    assert actions == []


def test_decision_direct_eval_no_match_falls_back() -> None:
    """槽位与任何分支都无证据交集 → 降级 LLM。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _DECISION_STEP},
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"employee_id": "1002"},
        ),
    )
    assert actions == []


def test_decision_direct_eval_negation_edge_never_selected() -> None:
    """含否定词的边不参与直判：高权限命中正向边，而非高权限边永不被选。"""
    edges = [
        {"next_node_id": "n6", "condition": "高权限"},
        {"next_node_id": "n3", "condition": "非高权限"},
    ]
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _DECISION_STEP},
            allowed_transitions=edges,
            known_slots={"access_level": "高权限"},
        ),
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "n6"
    # 反向：普通权限槽位无法为「非高权限」提供正向证据 → 降级
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _DECISION_STEP},
            allowed_transitions=edges,
            known_slots={"access_level": "普通查询"},
        ),
    )
    assert actions == []


def test_decision_direct_eval_requires_decision_type() -> None:
    """非 decision 类型节点不直判（保守门控）。"""
    step = dict(_DECISION_STEP, type="collect_info")
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": step},
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"access_level": "生产环境"},
        ),
    )
    assert actions == []


def test_decision_direct_eval_allowed_on_resume_recovery(monkeypatch) -> None:
    """恢复补槽帧槽位已齐时放行决策直判（死胡同修复后的新预期）。"""
    import app.core.sop.scenes as scenes_module

    def fail_llm(*args, **kwargs):
        raise AssertionError("恢复帧直判不应触发槽位抽取/LLM")

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fail_llm)

    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _DECISION_STEP},
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"access_level": "生产环境"},
        ),
        same_step=True,
        resumed_awaiting_user=True,
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "completed"
    assert actions[0]["next_step_id"] == "n6"
    assert events and events[0][1]["scene"] == "decision_direct_eval"


# ---------------------------------------------------------------------------
# P4 回归：脏槽位/消息冲突下的决策直判安全（17:17 截图复现）
# ---------------------------------------------------------------------------

_SLOT_FIELDS = ["employee_id", "system", "permission", "access_level", "permission_level"]


def test_decision_direct_eval_ignores_dirty_slots() -> None:
    """复现 17:17 截图：脏槽位 permission_preference 命中高权限边、合法字段
    permission=normal（英文）对不上中文条件 → 白名单过滤后 0 命中 → 降级 LLM。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={
                "skill_id": "sop-1",
                "step": _DECISION_STEP,
                "slot_fields": _SLOT_FIELDS,
            },
            allowed_transitions=_ROUTE_EDGES,
            known_slots={
                "employee_id": "1003",
                "system": "crm",
                "permission": "normal",
                "permission_level": "normal",
                "permission_preference": "倾向于申请生产环境的管理员权限",
            },
        ),
    )
    assert actions == []


def test_decision_direct_eval_message_conflict_veto() -> None:
    """合法槽位命中一条边，但用户消息逐字提到另一条边的条件 → 冲突否决。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={
                "skill_id": "sop-1",
                "step": _DECISION_STEP,
                "slot_fields": _SLOT_FIELDS,
            },
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"access_level": "生产环境"},
            source_user_message="申请生产环境crm的普通权限，工号1003",
        ),
    )
    assert actions == []


def test_handoff_summary_filters_dirty_slots() -> None:
    """F 回复摘要只含技能声明字段，脏槽位（permission_preference）不出现。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={
                "skill_id": "sop-1",
                "step": _HANDOFF_STEP,
                "slot_fields": _SLOT_FIELDS,
            },
            allowed_transitions=[],
            known_slots={
                "employee_id": "1001",
                "system": "crm",
                "permission_preference": "倾向于申请生产环境的管理员权限",
            },
        ),
    )
    assert len(actions) == 1
    reply = actions[0]["reply_fragment"]
    assert "1001" in reply
    assert "permission_preference" not in reply
    assert "倾向于" not in reply


# ---------------------------------------------------------------------------
# P4 场景 A2：会话复用时的新值合并（17:41 复测踩中旧工号问题）
# ---------------------------------------------------------------------------


def test_fresh_merge_updates_stale_slots_on_reused_session(monkeypatch) -> None:
    """复现 17:41 场景：session 槽位已齐（旧工号1003），新消息工号2003 →
    A2 抽取新值随 B 直通落库覆盖。"""
    import app.core.sop.scenes as scenes_module

    def fake_extract(*args, **kwargs):
        assert "employee_id" in args[0]
        return {"employee_id": "2003", "permission": "普通权限"}

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fake_extract)

    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={
                "skill_id": "sop-1",
                "step": {
                    "node_id": "n1",
                    "type": "collect",
                    "name": "收集申请信息",
                    "instruction": "收集工号、系统、权限级别。",
                    "expected_user_info": ["employee_id", "system", "permission", "access_level"],
                },
            },
            allowed_transitions=[{"next_node_id": "n3", "condition": ""}],
            known_slots={"employee_id": "1003", "system": "crm"},
            source_user_message="申请生产环境crm的普通权限，工号2003",
        ),
        slot_extraction_model="fake-model",
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "completed"
    assert actions[0]["next_step_id"] == "n3"
    assert actions[0]["slot_updates"] == {"employee_id": "2003", "permission": "普通权限"}


def test_fresh_merge_feeds_decision_evidence(monkeypatch) -> None:
    """A2 新值进入路由证据：旧槽位匹配不上，新值「普通权限」唯一命中 n3 边。"""
    import app.core.sop.scenes as scenes_module

    def fake_extract(*args, **kwargs):
        return {"permission": "普通权限"}

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fake_extract)

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={
                "skill_id": "sop-1",
                "step": dict(_DECISION_STEP, expected_user_info=["permission", "access_level"]),
                "slot_fields": _SLOT_FIELDS,
            },
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"employee_id": "1003", "permission": "normal"},
            source_user_message="申请crm的普通权限，工号2003",
        ),
        slot_extraction_model="fake-model",
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "n3"
    assert actions[0]["slot_updates"] == {"permission": "普通权限"}


def test_fresh_merge_conflict_message_falls_back(monkeypatch) -> None:
    """消息同时含两条边的条件词（普通权限+生产环境）→ 冲突否决，交还 LLM。"""
    import app.core.sop.scenes as scenes_module

    def fail_llm(*args, **kwargs):
        raise AssertionError("不应触发抽取")  # D 直判不调用抽取；A2 未配模型跳过

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fail_llm)

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={
                "skill_id": "sop-1",
                "step": _DECISION_STEP,
                "slot_fields": _SLOT_FIELDS,
            },
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"access_level": "生产环境"},
            source_user_message="申请生产环境crm的普通权限，工号2003",
        ),
    )
    assert actions == []


def test_fresh_merge_skipped_without_message_or_model(monkeypatch) -> None:
    """无新消息或未配抽取模型时不触发 A2（保持原 B 直通零开销）。"""
    import app.core.sop.scenes as scenes_module

    def fail_llm(*args, **kwargs):
        raise AssertionError("无消息/无模型不应触发抽取")

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fail_llm)

    base_kwargs = {
        "sop_context": {"skill_id": "sop-1", "step": dict(_DECISION_STEP, type="collect")},
        "allowed_transitions": [{"next_node_id": "n3", "condition": ""}],
        "known_slots": {"employee_id": "1003"},
    }
    # 无消息
    actions = plan_sop_prefill_actions(
        _sop_requirement(source_user_message="", **base_kwargs),
        slot_extraction_model="fake-model",
    )
    assert actions[0]["next_step_id"] == "n3"
    assert actions[0].get("slot_updates") in (None, {})
    # 无模型
    actions = plan_sop_prefill_actions(
        _sop_requirement(**base_kwargs),
    )
    assert actions[0]["next_step_id"] == "n3"
    assert actions[0].get("slot_updates") in (None, {})


# ---------------------------------------------------------------------------
# P4 场景 F：handoff 终点节点直通（零 LLM 转人工）
# ---------------------------------------------------------------------------

_HANDOFF_STEP = {
    "node_id": "n6",
    "type": "handoff",
    "name": "高权限人工审批",
    "instruction": "高权限申请转人工处理。",
}


def test_handoff_terminal_node_passthrough(monkeypatch) -> None:
    """终点 handoff 节点：槽位齐 + 无出边 → 零 LLM 直接 finish(handoff)。"""
    import app.core.sop.scenes as scenes_module

    def fail_llm(*args, **kwargs):
        raise AssertionError("handoff 直通不应触发槽位抽取/LLM")

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fail_llm)

    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "skill_name": "权限开通工单分流", "step": _HANDOFF_STEP},
            allowed_transitions=[],
            known_slots={
                "employee_id": "1001",
                "system": "crm",
                "permission": "管理员",
                "access_level": "生产环境",
            },
        ),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "handoff"
    assert action.get("next_step_id") in (None, "")
    assert "1001" in action["reply_fragment"]
    assert "crm" in action["reply_fragment"]
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["handoff_passthrough"]


def test_handoff_with_transitions_not_direct(monkeypatch) -> None:
    """handoff 节点带唯一无条件出边（非终点）→ 走场景 B 流转，不走 F。"""
    import app.core.sop.scenes as scenes_module

    def fail_llm(*args, **kwargs):
        raise AssertionError("不应触发抽取")

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fail_llm)

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _HANDOFF_STEP},
            allowed_transitions=[{"next_node_id": "n7", "condition": ""}],
            known_slots={"employee_id": "1001"},
        ),
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "completed"
    assert actions[0]["next_step_id"] == "n7"


def test_handoff_passthrough_allowed_on_resume_recovery() -> None:
    """恢复补槽帧槽位已齐时放行 handoff 直通（死胡同修复后的新预期）。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _HANDOFF_STEP},
            allowed_transitions=[],
            known_slots={"employee_id": "1001"},
        ),
        same_step=True,
        resumed_awaiting_user=True,
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "handoff"
    assert "1001" in actions[0]["reply_fragment"]


def test_handoff_without_slots_uses_generic_reply() -> None:
    """无已知槽位时用通用话术，不出现空括号。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _HANDOFF_STEP},
            allowed_transitions=[],
            known_slots={},
        ),
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "handoff"
    assert "（" not in actions[0]["reply_fragment"]


# ---------------------------------------------------------------------------
# P3 场景 E：tool_call 节点按 input_schema 槽位直组参数
# ---------------------------------------------------------------------------


def _tool_requirement(**overrides: Any) -> Any:
    from app.core.base.request_compiler import CapabilityDescriptor

    step = {
        "node_id": "n4",
        "type": "tool_call",
        "name": "调用权限开通接口",
        "instruction": "工具参数满足时直接调用 it.grant_permission 工具。",
    }
    if "step" in overrides:
        step = overrides.pop("step")
    manifest = overrides.pop(
        "capability_manifest",
        CapabilityManifest(
            available=[
                CapabilityDescriptor(
                    capability_id="cap-1",
                    name="it.grant_permission",
                    kind="tool",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "system": {"type": "string"},
                            "permission": {"type": "string"},
                            "access_level": {"type": "string"},
                        },
                        "required": ["system", "permission"],
                    },
                )
            ]
        ),
    )
    return _sop_requirement(
        sop_context={"skill_id": "sop-1", "step": step},
        capability_manifest=manifest,
        **overrides,
    )


def test_tool_direct_call_composes_args_from_slots() -> None:
    """schema 必填参数全部可由槽位满足 → 直接产出 tool 动作。"""
    events = _events()
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            required_capability_names=["it.grant_permission"],
            known_slots={
                "employee_id": "1002",
                "system": "crm",
                "permission": "管理员",
                "access_level": "生产环境",
            },
        ),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "tool"
    assert action["tool_name"] == "it.grant_permission"
    assert action["arguments"] == {
        "system": "crm",
        "permission": "管理员",
        "access_level": "生产环境",
    }
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["tool_direct_call"]


def test_tool_direct_call_missing_required_falls_back() -> None:
    """必填参数无槽位可组 → 降级 LLM，并留下 skipped 观测。"""
    events = _events()
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            required_capability_names=["it.grant_permission"],
            known_slots={"employee_id": "1002"},
        ),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert actions == []
    skipped = [p for t, p in events if t == TRACE_EVENT]
    assert skipped and skipped[0]["scene"] == "tool_direct_call_skipped"
    assert skipped[0]["missing_required"] == ["system", "permission"]


def test_tool_direct_call_requires_tool_call_type() -> None:
    step = {"node_id": "n4", "type": "decision", "name": "x"}
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            step=step,
            required_capability_names=["it.grant_permission"],
            known_slots={"system": "crm", "permission": "管理员"},
        ),
    )
    assert actions == []


def test_tool_direct_call_skipped_when_slots_missing() -> None:
    """槽位缺口未解除时不直组（先走收集/抽取链路）。"""
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            required_capability_names=["it.grant_permission"],
            required_slots=["system"],
            known_slots={},
        ),
    )
    assert actions == []


def test_tool_direct_call_ambiguous_capabilities_fall_back() -> None:
    """多个候选能力时不猜 → 降级 LLM。"""
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            required_capability_names=["it.grant_permission", "it.ticket_create"],
            known_slots={"system": "crm", "permission": "管理员"},
        ),
    )
    assert actions == []


def test_tool_direct_call_skips_internal_tools() -> None:
    """内置能力（knowledge_search 等）不走参数直组。"""
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            required_capability_names=["knowledge_search"],
            known_slots={"system": "crm", "permission": "管理员"},
        ),
    )
    assert actions == []


def test_harness_full_message_turn_completes_without_task_action_llm(monkeypatch) -> None:
    """集成（截图 bug 端到端）：首条消息带齐信息 + 抽齐 + 纯流转 →
    仅一次轻量抽取调用，零 task_action LLM 轮。"""
    from app.core.engine import step_loop as harness_agent_module
    from app.core.sop import scenes as scenes_module
    from app.db.models import ModelConfig

    def _fail_llm(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("抽取后直通不应触发 task_action LLM")

    monkeypatch.setattr(harness_agent_module, "LLMClient", _fail_llm)

    extract_calls: list[list[str]] = []

    def fake_extract(*args, **kwargs):
        extract_calls.append(list(kwargs.get("fields") or args[0]))
        return {"employee_id": "3012", "system": "OA", "permission": "管理员", "access_level": "生产"}

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", fake_extract)

    def _invoke_tool(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("不应调用工具")

    result = HarnessTaskAgent().run(
        _sop_requirement(
            required_slots=["employee_id", "system", "permission", "access_level"],
            expected_user_info=["employee_id", "system", "permission", "access_level"],
        ),
        ModelConfig(
            tenant_id="t", name="m", provider="openai", base_url="http://x/v1",
            api_key_encrypted="x", model_name="gpt", purpose="chat",
        ),
        _invoke_tool,
        max_actions=3,
    )

    assert result.status == "completed"
    assert result.next_step_id == "n3"
    assert result.slot_updates["employee_id"] == "3012"
    assert len(extract_calls) == 1
    # task_action LLM 轮次数：0（轻量抽取不算 task_action）


# ---------------------------------------------------------------------------
# 场景 G：结构化边条件求值直判（离线编译产物 + 零 LLM）
# ---------------------------------------------------------------------------


def _resolved_spec(kind: str, **kwargs: Any) -> dict[str, Any]:
    """模拟离线编译产物（``skill_edge_conditions.spec_json``）的载荷。"""

    return {"kind": kind, **kwargs}


def _specs_from_transitions(transitions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 ``condition_fingerprint`` 把条件装进指纹表——运行时真正的取用方式。"""

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


def test_spec_direct_eval_prefers_the_specific_branch_over_always() -> None:
    """if / else-if / else 语义：条件边命中时 `always` 兜底边让位。"""

    events = _events()
    transitions = [
        {
            "next_node_id": "n9",
            "condition": "所有必填信息都收集完成后进入",
            "condition_spec": _resolved_spec("slots_all", fields=["device_model", "urgency"]),
        },
        {"next_node_id": "n3", "condition": "", "condition_spec": _resolved_spec("always")},
    ]
    actions = plan_sop_prefill_actions(
        _sop_requirement(allowed_transitions=transitions, known_slots={"device_model": "X1", "urgency": "高"}),
        edge_condition_specs=_specs_from_transitions(transitions),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "n9"
    assert [p["scene"] for t, p in events if t == TRACE_EVENT] == ["condition_spec_direct_eval"]


def test_spec_direct_eval_falls_back_to_always_when_no_specific_matches() -> None:
    transitions = [
        {
            "next_node_id": "n9",
            "condition": "所有必填信息都收集完成后进入",
            "condition_spec": _resolved_spec("slots_all", fields=["device_model", "urgency"]),
        },
        {"next_node_id": "n3", "condition": "", "condition_spec": _resolved_spec("always")},
    ]
    actions = plan_sop_prefill_actions(
        _sop_requirement(allowed_transitions=transitions, known_slots={}),
        edge_condition_specs=_specs_from_transitions(transitions),
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "n3"


def test_spec_always_edge_is_unconditional_even_with_free_text() -> None:
    """无条件性由编译结果决定，不再依赖条件文本是不是空串。"""

    transitions = [
        {
            "next_node_id": "n3",
            "condition": "总是可进入",
            "condition_spec": _resolved_spec("always"),
        }
    ]
    actions = plan_sop_prefill_actions(
        _sop_requirement(allowed_transitions=transitions, known_slots={}),
        edge_condition_specs=_specs_from_transitions(transitions),
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "n3"


def test_result_ok_edge_uses_previous_capability_result() -> None:
    transitions = [
        {
            "next_node_id": "n9",
            "condition": "上一步工具调用成功后进入",
            "condition_spec": _resolved_spec("result_ok"),
        },
        {
            "next_node_id": "n8",
            "condition": "上一步工具调用失败后进入",
            "condition_spec": _resolved_spec("result_failed"),
        },
    ]
    specs = _specs_from_transitions(transitions)
    ok = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=transitions,
            prior_task_results=[{"capability_results": [{"tool_name": "x", "success": True}]}],
        ),
        edge_condition_specs=specs,
    )
    assert [action["next_step_id"] for action in ok] == ["n9"]
    failed = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=transitions,
            prior_task_results=[{"capability_results": [{"tool_name": "x", "success": False}]}],
        ),
        edge_condition_specs=specs,
    )
    assert [action["next_step_id"] for action in failed] == ["n8"]


def test_result_edge_defers_when_there_is_no_previous_result() -> None:
    """没有上一步调用结果 → 未知，绝不猜测（否则会把流程带到错误分支）。"""

    transitions = [
        {"next_node_id": "n9", "condition": "上一步工具调用成功后进入", "condition_spec": _resolved_spec("result_ok")},
        {"next_node_id": "n8", "condition": "上一步工具调用失败后进入", "condition_spec": _resolved_spec("result_failed")},
    ]
    assert (
        plan_sop_prefill_actions(
            _sop_requirement(allowed_transitions=transitions),
            edge_condition_specs=_specs_from_transitions(transitions),
        )
        == []
    )


def test_user_confirmed_edge_uses_current_message() -> None:
    transitions = [
        {"next_node_id": "n9", "condition": "用户明确确认后进入", "condition_spec": _resolved_spec("user_confirmed")},
        {"next_node_id": "n8", "condition": "用户明确拒绝后进入", "condition_spec": _resolved_spec("user_rejected")},
    ]
    specs = _specs_from_transitions(transitions)
    confirmed = plan_sop_prefill_actions(
        _sop_requirement(allowed_transitions=transitions, source_user_message="好的，确认提交"),
        edge_condition_specs=specs,
    )
    assert [action["next_step_id"] for action in confirmed] == ["n9"]
    rejected = plan_sop_prefill_actions(
        _sop_requirement(allowed_transitions=transitions, source_user_message="不用了，取消"),
        edge_condition_specs=specs,
    )
    assert [action["next_step_id"] for action in rejected] == ["n8"]


def test_unknown_edge_blocks_direct_eval() -> None:
    """有一条边求不出值（未编译 / llm_judge）→ 整体交还 LLM。"""

    transitions = [
        {
            "next_node_id": "n9",
            "condition": "所有必填信息都收集完成后进入",
            "condition_spec": _resolved_spec("slots_all", fields=["device_model"]),
        },
        {
            "next_node_id": "n8",
            "condition": "需要外部商品数据时进入",
            "condition_spec": _resolved_spec("llm_judge"),
        },
    ]
    assert (
        plan_sop_prefill_actions(
            _sop_requirement(allowed_transitions=transitions, known_slots={"device_model": "X1"}),
            edge_condition_specs=_specs_from_transitions(transitions),
        )
        == []
    )


def test_edge_without_compiled_spec_blocks_direct_eval() -> None:
    """部分编译的图同样不能直判——没编译的那条边可能才是该走的。"""

    transitions = [
        {
            "next_node_id": "n9",
            "condition": "所有必填信息都收集完成后进入",
            "condition_spec": _resolved_spec("slots_all", fields=["device_model"]),
        },
        {"next_node_id": "n8", "condition": "需要外部商品数据时进入"},
    ]
    assert (
        plan_sop_prefill_actions(
            _sop_requirement(allowed_transitions=transitions, known_slots={"device_model": "X1"}),
            edge_condition_specs=_specs_from_transitions(transitions),
        )
        == []
    )


def test_two_specific_matches_defer_to_llm() -> None:
    transitions = [
        {"next_node_id": "n9", "condition": "a", "condition_spec": _resolved_spec("always")},
        {"next_node_id": "n8", "condition": "b", "condition_spec": _resolved_spec("user_confirmed")},
        {"next_node_id": "n7", "condition": "c", "condition_spec": _resolved_spec("slots_missing", fields=["device_model"])},
    ]
    # user_confirmed 命中（消息含「确认」），slots_missing 也命中（device_model 为空）
    assert (
        plan_sop_prefill_actions(
            _sop_requirement(
                allowed_transitions=transitions,
                source_user_message="确认一下",
                known_slots={},
            ),
            edge_condition_specs=_specs_from_transitions(transitions),
        )
        == []
    )


def test_decision_node_with_compiled_edges_skips_legacy_token_matching() -> None:
    """编译过的 decision 节点走场景 G，不再用中文词元包含做匹配。"""

    transitions = [
        {
            "next_node_id": "n9",
            "condition": "当前节点没有任何缺失字段时进入",
            "condition_spec": _resolved_spec("slots_all", fields=["device_model"]),
        },
        {"next_node_id": "n8", "condition": "device_model 缺失时进入"},
    ]
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=transitions,
            known_slots={"device_model": "X1"},
            expected_user_info=[],
        ),
        edge_condition_specs=_specs_from_transitions(transitions),
    )
    # n8 未编译 → 未知 → 交还 LLM（旧场景 D 会凭 "device_model" 词元误判成 n8）
    assert actions == []


def test_legacy_decision_direct_eval_still_works_without_specs() -> None:
    """一条都没编译过时，旧场景 D 行为保持不变（兼容未回填的历史技能）。"""

    transitions = [
        {"next_node_id": "n9", "condition": "生产环境管理员"},
        {"next_node_id": "n8", "condition": "普通权限"},
    ]
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=transitions,
            known_slots={"access_level": "生产环境管理员"},
            expected_user_info=[],
            sop_context={
                "skill_id": "sop-1",
                "step": {
                    "node_id": "n2",
                    "type": "decision",
                    "name": "判断",
                    "expected_user_info": [],
                },
            },
        )
    )
    assert [action["next_step_id"] for action in actions] == ["n9"]


def test_after_extraction_uses_collected_slots_for_condition_eval(monkeypatch) -> None:
    """槽位抽齐后立刻用完整槽位求值条件边——最常见分支模式不再回落 LLM。"""

    import app.core.sop.scenes as scenes_module

    monkeypatch.setattr(
        scenes_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: {"device_model": "X1", "urgency": "高"},
    )
    transitions = [
        {
            "next_node_id": "n9",
            "condition": "所有必填信息都收集完成后进入",
            "condition_spec": _resolved_spec("slots_all", fields=["device_model", "urgency"]),
        },
        {"next_node_id": "n2", "condition": "", "condition_spec": _resolved_spec("always")},
    ]
    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=transitions,
            required_slots=["device_model", "urgency"],
            expected_user_info=["device_model", "urgency"],
        ),
        slot_extraction_model="fake-model",
        edge_condition_specs=_specs_from_transitions(transitions),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "n9"
    assert actions[0]["slot_updates"] == {"device_model": "X1", "urgency": "高"}
    assert [p["scene"] for t, p in events if t == TRACE_EVENT] == [
        "condition_spec_direct_eval_after_extraction"
    ]


def test_scene_g_does_not_fire_before_slot_extraction() -> None:
    """槽位还没抽齐时不直判（先把信息收齐，别抢跑）。"""

    transitions = [
        {
            "next_node_id": "n9",
            "condition": "所有必填信息都收集完成后进入",
            "condition_spec": _resolved_spec("slots_all", fields=["device_model"]),
        }
    ]
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=transitions,
            required_slots=["device_model"],
            expected_user_info=["device_model"],
        ),
        edge_condition_specs=_specs_from_transitions(transitions),
    )
    # 无抽取模型 → 走场景 A 的模板询问，不是条件直判
    assert actions and actions[0]["status"] == "awaiting_user"


def test_broken_specs_payload_degrades_to_llm() -> None:
    """编译产物载荷损坏（脏数据）时按「未编译」处理，不抛异常。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(allowed_transitions=[{"next_node_id": "n9", "condition": "用户确认后"}]),
        edge_condition_specs={"deadbeef": {"kind": "不存在的类型"}},
    )
    assert actions == []


def test_harness_agent_passes_edge_condition_specs_to_executor() -> None:
    """集成：HarnessTaskAgent 必须把编译产物透传到执行器（否则场景 G 空转）。"""

    captured: list[Any] = []
    import app.core.engine.step_loop as harness_module
    from app.db.models import ModelConfig

    original = harness_module.plan_sop_prefill_actions

    def _spy(requirement, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs.get("edge_condition_specs"))
        return original(requirement, **kwargs)

    harness_module.plan_sop_prefill_actions = _spy  # type: ignore[assignment]
    try:
        transitions = [
            {
                "next_node_id": "n9",
                "condition": "所有必填信息都收集完成后进入",
                "condition_spec": _resolved_spec("slots_all", fields=["device_model"]),
            }
        ]
        specs = _specs_from_transitions(transitions)
        result = HarnessTaskAgent().run(
            _sop_requirement(
                allowed_transitions=transitions,
                known_slots={"device_model": "X1"},
            ),
            ModelConfig(
                tenant_id="t", name="m", provider="openai", base_url="http://x/v1",
                api_key_encrypted="x", model_name="gpt", purpose="chat",
            ),
            lambda name, args: {"success": True, "data": {}},
            max_actions=1,
            edge_condition_specs=specs,
        )
    finally:
        harness_module.plan_sop_prefill_actions = original  # type: ignore[assignment]

    assert captured == [specs]
    assert result.next_step_id == "n9"


# ---------------------------------------------------------------------------
# A2UI（MVP）：缺槽询问附带的表单描述 + 表单提交的确定性写槽
# ---------------------------------------------------------------------------


def test_await_user_action_carries_slot_form() -> None:
    """缺槽询问除文案外还要带一份表单描述（前端渲染成控件）。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model", "urgency"]),
    )
    assert len(actions) == 1
    form = actions[0]["ui_form"]
    assert form is not None
    assert form["kind"] == "slot_form"
    assert form["step_id"] == "n2"
    assert [field["name"] for field in form["fields"]] == ["device_model", "urgency"]
    assert form["fields"][0]["label"] == "设备型号"
    # 文案仍然保留：非 UI 渠道（企微/微信）只收文本
    assert "设备型号" in actions[0]["reply_fragment"]


def test_await_user_action_confirm_step_renders_buttons() -> None:
    """确认型步骤（缺的全是 confirm 字段）：话术按确认写，表单是按钮不是文本框。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["confirm_action"],
            expected_user_info=["confirm_action"],
        ),
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "awaiting_user"
    assert "请确认「提交申请」" in actions[0]["reply_fragment"]
    assert "请提供" not in actions[0]["reply_fragment"]
    form = actions[0]["ui_form"]
    assert form is not None
    assert form["title"] == "提交申请"
    field = form["fields"][0]
    assert field["type"] == "confirm"
    assert [option["value"] for option in field["options"]] == ["确认", "取消"]


def test_no_slot_form_when_nothing_missing() -> None:
    """无缺槽时不该出现表单（纯流转直通是零 LLM 的，别塞多余载荷）。"""

    actions = plan_sop_prefill_actions(_sop_requirement(required_slots=[]))
    assert actions[0]["status"] == "completed"
    assert actions[0].get("ui_form") is None


def test_slot_submission_completes_slots_without_llm(monkeypatch) -> None:
    """表单提交 → 直接写槽推进，**不调用**槽位抽取 LLM。"""

    import app.core.sop.scenes as scenes_module

    calls: list[Any] = []

    def _spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        raise AssertionError("表单提交路径不应该调用槽位抽取 LLM")

    monkeypatch.setattr(scenes_module, "_extract_slots_llm", _spy)

    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system"],
            expected_user_info=["employee_id", "system"],
        ),
        slot_extraction_model="fake-model",
        slot_submission={"employee_id": " 3012 ", "system": "OA系统"},
        trace_sink=lambda t, p: events.append((t, p)),
    )

    assert calls == []
    assert len(actions) == 1
    action = actions[0]
    assert action["status"] == "completed"
    assert action["next_step_id"] == "n3"
    assert action["slot_updates"] == {"employee_id": "3012", "system": "OA系统"}
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["passthrough_transition_after_extraction"]


def test_slot_submission_partial_asks_only_missing(monkeypatch) -> None:
    """表单只填了一部分：只问缺的，已提交的值随 slot_updates 落库。"""

    import app.core.sop.scenes as scenes_module

    monkeypatch.setattr(
        scenes_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: pytest.fail("表单提交路径不应调用抽取 LLM"),
    )

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system"],
            expected_user_info=["employee_id", "system"],
        ),
        slot_extraction_model="fake-model",
        slot_submission={"employee_id": "3012"},
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["status"] == "awaiting_user"
    assert action["slot_updates"] == {"employee_id": "3012"}
    assert "目标系统" in action["reply_fragment"]
    assert "员工工号" not in action["reply_fragment"]
    # 补问缺的那个字段时还要继续给表单，否则用户只能退回打字
    assert [field["name"] for field in action["ui_form"]["fields"]] == ["system"]


def test_slot_submission_drops_undeclared_and_blank_fields(monkeypatch) -> None:
    """不在本节点声明里的键、以及空值：一律不算「已提供」。"""

    import app.core.sop.scenes as scenes_module

    monkeypatch.setattr(
        scenes_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: pytest.fail("表单提交路径不应调用抽取 LLM"),
    )

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system"],
            expected_user_info=["employee_id", "system"],
        ),
        slot_extraction_model="fake-model",
        slot_submission={
            "employee_id": "3012",
            "system": "   ",
            "rogue_field": "注入",
        },
    )
    action = actions[0]
    assert action["status"] == "awaiting_user"
    assert action["slot_updates"] == {"employee_id": "3012"}
    assert "rogue_field" not in action["slot_updates"]


def test_slot_submission_normalizes_key_case_and_separators(monkeypatch) -> None:
    """前端把字段名写成 camelCase / 连字符也要能对上声明字段。"""

    import app.core.sop.scenes as scenes_module

    monkeypatch.setattr(
        scenes_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: pytest.fail("表单提交路径不应调用抽取 LLM"),
    )

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "device_model"],
            expected_user_info=["employee_id", "device_model"],
        ),
        slot_extraction_model="fake-model",
        slot_submission={"employeeId": "3012", "Device-Model": "X1"},
    )
    assert actions[0]["status"] == "completed"
    assert actions[0]["slot_updates"] == {"employee_id": "3012", "device_model": "X1"}


def test_slot_submission_without_model_still_works() -> None:
    """没有轻量模型（未配置意图模型）时表单路径照样能跑——它本来就不需要 LLM。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id"],
            expected_user_info=["employee_id"],
        ),
        slot_extraction_model=None,
        slot_submission={"employee_id": "3012"},
    )
    assert actions[0]["status"] == "completed"
    assert actions[0]["slot_updates"] == {"employee_id": "3012"}


def test_harness_agent_passes_slot_submission_to_executor() -> None:
    """集成：表单提交必须被透传到执行器（否则等于白提交，还会回落 LLM 抽取）。"""

    captured: list[Any] = []
    import app.core.engine.step_loop as harness_module
    from app.db.models import ModelConfig

    original = harness_module.plan_sop_prefill_actions

    def _spy(requirement, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs.get("slot_submission"))
        return original(requirement, **kwargs)

    harness_module.plan_sop_prefill_actions = _spy  # type: ignore[assignment]
    try:
        result = HarnessTaskAgent().run(
            _sop_requirement(
                required_slots=["device_model"],
                expected_user_info=["device_model"],
            ),
            ModelConfig(
                tenant_id="t", name="m", provider="openai", base_url="http://x/v1",
                api_key_encrypted="x", model_name="gpt", purpose="chat",
            ),
            lambda name, args: {"success": True, "data": {}},
            max_actions=1,
            slot_submission={"device_model": "X1"},
        )
    finally:
        harness_module.plan_sop_prefill_actions = original  # type: ignore[assignment]

    assert captured == [{"device_model": "X1"}]
    assert result.status == "completed"
    assert result.next_step_id == "n3"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])


