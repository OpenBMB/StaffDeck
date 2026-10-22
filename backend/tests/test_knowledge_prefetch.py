"""仅知识库数字员工确定性预检索（conversation 直查）测试。

覆盖触发条件、窄场景边界（有技能/工具不介入、SOP 不介入、续轮不介入）、
确定性判定（寒暄词典是唯一闸门，不再调 LLM）与降级行为；集成行为
（预填动作被 harness 消费、跳过首轮 LLM 决策）复用
test_sop_step_executor.py 的 HarnessTaskAgent 测试范式。
"""

from __future__ import annotations

from typing import Any

from app.core.knowledge_flow.prefetch import (
    QUERY_MAX_CHARS,
    plan_knowledge_prefill_actions,
)
from app.core.base.request_compiler import CapabilityDescriptor, CapabilityManifest

import app.core.knowledge_flow.prefetch as kf  # noqa: E402


def _descriptor(
    name: str,
    kind: str = "internal",
    *,
    available: bool = True,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=f"cap-{name}",
        name=name,
        kind=kind,  # type: ignore[arg-type]
        available=available,
    )


def _requirement(
    *,
    kind: str = "conversation",
    message: str = "公司的年假政策是什么？",
    manifest: CapabilityManifest | None = None,
    required_knowledge_base_ids: list[str] | None = None,
    **overrides: Any,
) -> Any:
    """构造最小 requirement（用 SimpleNamespace 避免 TaskRequirement 的 kind 字面量约束）。"""

    from types import SimpleNamespace

    return SimpleNamespace(
        task_frame_id="task-1",
        kind=kind,
        goal="回答用户问题",
        source_user_message=message,
        required_knowledge_base_ids=required_knowledge_base_ids or [],
        capability_manifest=manifest
        or CapabilityManifest(available=[_descriptor("knowledge_search")]),
        **overrides,
    )


# ---------------------------------------------------------------------------
# 触发
# ---------------------------------------------------------------------------


def test_prefills_knowledge_search_for_kb_only_conversation() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    actions = plan_knowledge_prefill_actions(
        _requirement(),
        first_turn=True,
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "tool"
    assert action["tool_name"] == "knowledge_search"
    assert action["arguments"]["query"] == "公司的年假政策是什么？"
    assert action["arguments"]["max_chunks"] == 8
    # 不带 knowledge_base_ids：invoker 默认取 TaskFrame 全部授权范围
    assert "knowledge_base_ids" not in action["arguments"]
    # 判定已纯确定性：只写预填事件，不再有 _judge / _judge_error
    assert [t for t, _ in events] == [kf.TRACE_EVENT]
    assert events[0][1]["query"] == "公司的年假政策是什么？"


def test_query_truncated_to_max_chars() -> None:
    long_message = "长" * 500
    actions = plan_knowledge_prefill_actions(_requirement(message=long_message))
    assert actions[0]["arguments"]["query"] == "长" * QUERY_MAX_CHARS


# ---------------------------------------------------------------------------
# 窄场景边界
# ---------------------------------------------------------------------------


def test_skipped_for_sop_requirement() -> None:
    """SOP 需求由 sop_step_executor 场景 C 负责，这里不介入。"""
    assert (
        plan_knowledge_prefill_actions(_requirement(kind="sop"), first_turn=True) == []
    )


def test_skipped_when_general_skill_capability_present() -> None:
    manifest = CapabilityManifest(
        available=[
            _descriptor("knowledge_search"),
            _descriptor("leave_skill", "general_skill"),
        ]
    )
    assert plan_knowledge_prefill_actions(_requirement(manifest=manifest)) == []


def test_skipped_when_sop_specific_capability_present() -> None:
    """SOP 专属技能的 kind 也是 general_skill（scope 区分），同样阻断。"""
    manifest = CapabilityManifest(
        available=[
            _descriptor("knowledge_search"),
            CapabilityDescriptor(
                capability_id="cap-sop-skill",
                name="sop_skill",
                kind="general_skill",
                capability_scope="sop_specific",
                available=True,
            ),
        ]
    )
    assert plan_knowledge_prefill_actions(_requirement(manifest=manifest)) == []


def test_skipped_when_tool_capability_present() -> None:
    manifest = CapabilityManifest(
        available=[
            _descriptor("knowledge_search"),
            _descriptor("crm_query", "tool"),
        ]
    )
    assert plan_knowledge_prefill_actions(_requirement(manifest=manifest)) == []


def test_file_and_unavailable_capabilities_do_not_block() -> None:
    """file 是附件读取（与预填正交）；不可用能力不参与判定。"""
    manifest = CapabilityManifest(
        available=[
            _descriptor("knowledge_search"),
            _descriptor("read_attachment", "file"),
            _descriptor("dead_skill", "general_skill", available=False),
        ]
    )
    actions = plan_knowledge_prefill_actions(_requirement(manifest=manifest))
    assert len(actions) == 1


def test_skipped_when_no_knowledge_search_authorization() -> None:
    manifest = CapabilityManifest(available=[_descriptor("session_title")])
    assert plan_knowledge_prefill_actions(_requirement(manifest=manifest)) == []


def test_skipped_on_later_turn() -> None:
    assert (
        plan_knowledge_prefill_actions(_requirement(), first_turn=False) == []
    )


def test_skipped_when_required_kb_ids_present() -> None:
    assert (
        plan_knowledge_prefill_actions(
            _requirement(required_knowledge_base_ids=["kb-1"])
        )
        == []
    )


def test_skipped_for_tiny_message() -> None:
    assert plan_knowledge_prefill_actions(_requirement(message="你")) == []
    assert plan_knowledge_prefill_actions(_requirement(message="")) == []
    assert plan_knowledge_prefill_actions(_requirement(message="  ")) == []


# ---------------------------------------------------------------------------
# 确定性判定：寒暄词典是唯一闸门（不再有 LLM judge）
# ---------------------------------------------------------------------------


def test_smalltalk_dictionary_blocks_prefill() -> None:
    """纯寒暄/纯礼貌语（去标点/语气词后完全匹配）零成本拦截。"""

    for msg in (
        "hi", "Hi!", "你好", "你好呀", "在吗？", "hello there",
        "哈喽~", "哈喽", "你好哇", "谢谢", "谢谢啦", "辛苦了", "再见", "收到",
    ):
        assert plan_knowledge_prefill_actions(_requirement(message=msg)) == []


def test_smalltalk_dictionary_never_prefix_matches() -> None:
    """寒暄+真问题的混合消息不命中词典 → 照常预填，query 原样透传。"""

    actions = plan_knowledge_prefill_actions(
        _requirement(message="你好，我想问下年假政策")
    )
    assert len(actions) == 1
    assert actions[0]["arguments"]["query"] == "你好，我想问下年假政策"


def test_non_smalltalk_always_prefills_without_llm() -> None:
    """移除 LLM judge 后的核心契约：非寒暄消息一律预填，不依赖任何模型。

    复现线上现场：'那身高体重有要求吗???'（追问/指代句）曾被 judge 判 no
    而退回一整轮决策；现在应直接预填。
    """

    assert not hasattr(kf, "_judge_needs_search")  # 判定器已移除
    for msg in ("那身高体重有要求吗???", "有什么娱乐设施", "项目在哪", "现在几点了"):
        actions = plan_knowledge_prefill_actions(_requirement(message=msg))
        assert len(actions) == 1, msg
        assert actions[0]["arguments"]["query"] == msg


def test_degrades_silently_on_exception() -> None:
    class _Boom:
        kind = "conversation"

        @property
        def capability_manifest(self) -> Any:
            raise RuntimeError("boom")

    assert plan_knowledge_prefill_actions(_Boom()) == []


def test_trace_sink_failure_does_not_block_prefill() -> None:
    def _broken_sink(topic: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("sink down")

    actions = plan_knowledge_prefill_actions(
        _requirement(), trace_sink=_broken_sink
    )
    assert len(actions) == 1


# ---------------------------------------------------------------------------
# 集成：HarnessTaskAgent 消费预填动作，跳过首轮 LLM 决策
# ---------------------------------------------------------------------------


def test_harness_conversation_prefetch_executes_before_first_llm_call(
    monkeypatch,
) -> None:
    from app.core import harness_agent as harness_agent_module
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
                "reply_fragment": "年假政策如下……",
                "task_summary": "完成。",
            }

    monkeypatch.setattr(harness_agent_module, "LLMClient", FakeLLMClient)

    tool_calls: list[tuple[str, dict[str, Any]]] = []

    def _invoke_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool_calls.append((name, arguments))
        return {
            "success": True,
            "data": {"evidence_pack": [{"content": "年假：入职满一年 5 天……"}]},
            "citations": [{"source": "kb-hr"}],
        }

    trace_events: list[tuple[str, dict[str, Any]]] = []
    result = harness_agent_module.HarnessTaskAgent().run(
        harness_agent_module.TaskRequirement(
            task_frame_id="task-1",
            kind="conversation",
            goal="回答用户问题",
            source_user_message="公司的年假政策是什么？",
            capability_manifest=CapabilityManifest(
                available=[_descriptor("knowledge_search")]
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

    # 预填检索先执行，LLM 首轮直接基于证据收尾：整条链路仅 1 次 LLM 调用
    assert result.status == "completed"
    assert result.reply_fragment == "年假政策如下……"
    assert len(llm_calls) == 1
    assert tool_calls == [
        ("knowledge_search", {"query": "公司的年假政策是什么？", "max_chunks": 8})
    ]
    prefetch_events = [
        payload for event_type, payload in trace_events if event_type == kf.TRACE_EVENT
    ]
    assert prefetch_events and prefetch_events[0]["query"] == "公司的年假政策是什么？"


def test_harness_conversation_with_skill_capability_skips_prefetch(
    monkeypatch,
) -> None:
    """有技能能力（非纯知识库）时不预填：LLM 首轮决策照旧。"""
    from app.core import harness_agent as harness_agent_module
    from app.db.models import ModelConfig
    from app.core.base.request_compiler import CapabilityManifest

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
                "reply_fragment": "直接回答。",
                "task_summary": "完成。",
            }

    monkeypatch.setattr(harness_agent_module, "LLMClient", FakeLLMClient)

    tool_calls: list[tuple[str, dict[str, Any]]] = []

    def _invoke_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool_calls.append((name, arguments))
        return {"success": True, "data": {}}

    result = harness_agent_module.HarnessTaskAgent().run(
        harness_agent_module.TaskRequirement(
            task_frame_id="task-2",
            kind="conversation",
            goal="回答用户问题",
            source_user_message="公司的年假政策是什么？",
            capability_manifest=CapabilityManifest(
                available=[
                    _descriptor("knowledge_search"),
                    _descriptor("leave_skill", "general_skill"),
                ]
            ),
        ),
        ModelConfig(
            tenant_id="t", name="m", provider="openai", base_url="http://x/v1",
            api_key_encrypted="x", model_name="gpt", purpose="chat",
        ),
        _invoke_tool,
        max_actions=3,
    )

    assert result.status == "completed"
    assert tool_calls == []  # 没有预填：工具由模型自己决定调不调
    assert len(llm_calls) == 1