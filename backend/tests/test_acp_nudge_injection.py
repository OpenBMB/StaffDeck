"""U6 nudge injection tests: session-layer and task-layer advisory nudges.

Covers pressure evaluation against real usage (the meter source) with the
pre-check estimate as fallback, threshold crossing (normal vs emergency
copy), and the guarantee that nudges appear only in ACP mode.
"""

from copy import deepcopy
from types import SimpleNamespace

from app.core import agent_loop as agent_loop_module
from app.core import harness_agent as harness_agent_module
from app.core.acp import AcpConfig, AcpEngine
from app.core.acp.nudge import NudgeRecommendation
from app.core.acp.pricing import RealUsageMeter
from app.core.agent_loop import (
    AgentLoop,
    _acp_nudge_message,
    _attach_acp_nudge,
)
from app.core.capability_manifest import _acp_capability_descriptors
from app.core.conversation_context import build_conversation_context
from app.core.harness_agent import HarnessTaskAgent, _acp_task_nudge
from app.core.task_request_compiler import (
    CapabilityDescriptor,
    CapabilityManifest,
    TaskRequirement,
)
from app.db.models import ModelConfig
from app.llm.stage_protocol import STAGE_PROTOCOL_KEY, stage_payload

HIGH_USAGE = {"input_tokens": 100_000, "source_chars": 400_000}
EMERGENCY_USAGE = {"input_tokens": 115_000, "source_chars": 460_000}
LOW_USAGE = {"input_tokens": 30_000, "source_chars": 120_000}


def _model_config() -> ModelConfig:
    return ModelConfig(
        id="model-test",
        tenant_id="tenant-demo",
        name="测试模型",
        api_key_encrypted="test",
        model="test-model",
    )


def _acp_requirement(*, include_acp: bool = True) -> TaskRequirement:
    available: list[CapabilityDescriptor] = [
        CapabilityDescriptor(
            capability_id="allowed",
            name="allowed.tool",
            kind="tool",
        )
    ]
    if include_acp:
        available.extend(_acp_capability_descriptors())
    return TaskRequirement(
        task_frame_id="task-1",
        kind="conversation",
        goal="查询物流",
        requirements=["查询 ORDER-1 的物流"],
        capability_manifest=CapabilityManifest(available=available),
    )


def _fake_llm(monkeypatch, actions, payloads: list | None = None):
    class FakeLLMClient:
        def __init__(self, _model_config: ModelConfig, session_id: str | None = None):
            pass

        def generate_json(
            self, system_prompt: str, payload: dict[str, object]
        ) -> dict[str, object]:
            if payloads is not None:
                payloads.append(deepcopy(payload))
            return next(actions)

    monkeypatch.setattr(harness_agent_module, "LLMClient", FakeLLMClient)


def _tool_invoke():
    def invoke_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "success": True,
            "data": {"status": "in_transit", "note": "ORDER-1 已发货"},
        }

    return invoke_tool


def _fake_db():
    return SimpleNamespace(
        get=lambda _model, _key: None,
        exec=lambda _stmt: SimpleNamespace(first=lambda: None),
    )


def _finish_action() -> dict[str, object]:
    return {
        "action": "finish",
        "status": "completed",
        "reply_fragment": "完成。",
        "task_summary": "完成。",
    }


# -- session-layer nudge helpers -------------------------------------------


def test_acp_nudge_message_normal_and_emergency_copy() -> None:
    normal = NudgeRecommendation(
        level="normal",
        current_tokens=90_000,
        limit_tokens=128_000,
        usage_pct=0.70,
        message="提示：上下文使用率已达 70%，可考虑压缩历史消息（非强制）。",
    )
    emergency = NudgeRecommendation(
        level="emergency",
        current_tokens=110_000,
        limit_tokens=128_000,
        usage_pct=0.86,
        message="紧急：上下文使用率已达 86%，建议立即压缩历史消息（非强制）。",
    )
    normal_text = _acp_nudge_message(normal)
    emergency_text = _acp_nudge_message(emergency)
    assert "非强制" in normal_text
    assert "acp_status" in normal_text
    assert "紧急" in emergency_text
    assert "非强制" in emergency_text
    assert "acp_decompress" in emergency_text


def test_session_nudge_uses_real_usage_when_available() -> None:
    engine = AcpEngine(
        config=AcpConfig(model_context_limit=1_000, nudge_max_context_limit_pct=0.5)
    )
    meter = RealUsageMeter()
    meter.record_usage(input_tokens=600, source_chars=2_400)
    context: dict[str, object] = {"metadata": {"estimated_tokens": 100}}
    _attach_acp_nudge(context, engine, meter)
    assert context["nudge"]["estimated"] is False
    assert context["nudge"]["current_tokens"] == 600
    assert context["nudge"]["level"] == "normal"


def test_session_nudge_falls_back_to_estimate_with_flag() -> None:
    engine = AcpEngine(
        config=AcpConfig(model_context_limit=1_000, nudge_max_context_limit_pct=0.5)
    )
    meter = RealUsageMeter()
    context: dict[str, object] = {"metadata": {"estimated_tokens": 600}}
    _attach_acp_nudge(context, engine, meter)
    assert context["nudge"]["estimated"] is True
    assert context["nudge"]["current_tokens"] == 600
    assert context["nudge"]["level"] == "normal"


def test_session_nudge_absent_below_threshold() -> None:
    engine = AcpEngine(
        config=AcpConfig(model_context_limit=1_000, nudge_max_context_limit_pct=0.5)
    )
    meter = RealUsageMeter()
    meter.record_usage(input_tokens=100, source_chars=400)
    context: dict[str, object] = {"metadata": {"estimated_tokens": 100}}
    _attach_acp_nudge(context, engine, meter)
    assert "nudge" not in context


def test_session_nudge_emergency_escalates() -> None:
    engine = AcpEngine(
        config=AcpConfig(
            model_context_limit=1_000,
            nudge_max_context_limit_pct=0.5,
            nudge_emergency_threshold_pct=0.8,
        )
    )
    meter = RealUsageMeter()
    meter.record_usage(input_tokens=900, source_chars=3_600)
    context: dict[str, object] = {"metadata": {"estimated_tokens": 100}}
    _attach_acp_nudge(context, engine, meter)
    assert context["nudge"]["level"] == "emergency"
    assert "紧急" in context["nudge"]["message"]


# -- session-layer integration ---------------------------------------------


def test_session_context_attaches_nudge_when_real_usage_crosses_threshold(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        agent_loop_module, "latest_llm_usage_observation", lambda _session_id=None: HIGH_USAGE
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _fake_db()
    chat_session = SimpleNamespace(
        tenant_id="tenant_test",
        agent_id=None,
        id="session_1",
        context_state_json=None,
    )
    context = AgentLoop._acp_conversation_context(
        loop,
        chat_session,
        [{"id": "m1", "role": "user", "content": "你好"}],
    )
    assert context["nudge"]["level"] == "normal"
    assert context["nudge"]["estimated"] is False
    assert "非强制" in context["nudge"]["message"]


def test_session_context_no_nudge_below_threshold(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_loop_module, "latest_llm_usage_observation", lambda _session_id=None: LOW_USAGE
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _fake_db()
    chat_session = SimpleNamespace(
        tenant_id="tenant_test",
        agent_id=None,
        id="session_1",
        context_state_json=None,
    )
    context = AgentLoop._acp_conversation_context(
        loop,
        chat_session,
        [{"id": "m1", "role": "user", "content": "你好"}],
    )
    assert "nudge" not in context


def test_session_stage_payload_contains_nudge_only_in_acp_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_loop_module, "latest_llm_usage_observation", lambda _session_id=None: HIGH_USAGE
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _fake_db()
    chat_session = SimpleNamespace(
        tenant_id="tenant_test",
        agent_id=None,
        id="session_1",
        context_state_json=None,
    )
    acp_context = AgentLoop._acp_conversation_context(
        loop,
        chat_session,
        [{"id": "m1", "role": "user", "content": "你好"}],
    )
    payload = stage_payload(
        phase="Router",
        user_message="你好",
        conversation_context=acp_context,
        memory_context=None,
        instructions="阶段规则原文",
        stage_data={},
        output_contract="{}",
    )
    instructions = payload[STAGE_PROTOCOL_KEY]["instructions"]
    assert "阶段规则原文" in instructions
    assert "非强制" in instructions
    assert "acp_status" in instructions

    legacy_context = build_conversation_context([{"role": "user", "content": "你好"}])
    assert "nudge" not in legacy_context
    legacy_payload = stage_payload(
        phase="Router",
        user_message="你好",
        conversation_context=legacy_context,
        memory_context=None,
        instructions="阶段规则原文",
        stage_data={},
        output_contract="{}",
    )
    assert "非强制" not in legacy_payload[STAGE_PROTOCOL_KEY]["instructions"]


def test_stage_payload_ignores_malformed_nudge() -> None:
    payload = stage_payload(
        phase="Router",
        user_message="你好",
        conversation_context={"nudge": "not-a-dict"},
        memory_context=None,
        instructions="阶段规则原文",
        stage_data={},
        output_contract="{}",
    )
    assert payload[STAGE_PROTOCOL_KEY]["instructions"] == "阶段规则原文"


# -- task-layer nudge ------------------------------------------------------


def test_task_nudge_uses_real_usage_when_available() -> None:
    engine = AcpEngine(
        config=AcpConfig(model_context_limit=1_000, nudge_max_context_limit_pct=0.5)
    )
    meter = RealUsageMeter()
    meter.record_usage(input_tokens=600, source_chars=2_400)
    nudge = _acp_task_nudge(engine, meter, [])
    assert nudge is not None
    assert nudge["estimated"] is False
    assert nudge["current_tokens"] == 600
    assert nudge["level"] == "normal"


def test_task_nudge_falls_back_to_estimate_with_flag() -> None:
    engine = AcpEngine()
    meter = RealUsageMeter()
    transcript = [
        {
            "role": "tool",
            "tool_name": "allowed.tool",
            "result": {"success": True, "data": {"note": "内容" * 5_000}},
        }
        for _ in range(40)
    ]
    nudge = _acp_task_nudge(engine, meter, transcript)
    assert nudge is not None
    assert nudge["estimated"] is True
    assert nudge["level"] == "normal"
    assert "非强制" in nudge["message"]


def test_task_nudge_absent_below_threshold() -> None:
    engine = AcpEngine()
    meter = RealUsageMeter()
    meter.record_usage(input_tokens=30_000, source_chars=120_000)
    assert _acp_task_nudge(engine, meter, []) is None


def test_task_nudge_emergency_escalates() -> None:
    engine = AcpEngine()
    meter = RealUsageMeter()
    meter.record_usage(input_tokens=115_000, source_chars=460_000)
    nudge = _acp_task_nudge(engine, meter, [])
    assert nudge is not None
    assert nudge["level"] == "emergency"
    assert "紧急" in nudge["message"]


def test_task_payload_contains_nudge_only_in_acp_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        harness_agent_module, "latest_llm_usage_observation", lambda _session_id=None: HIGH_USAGE
    )
    payloads: list[dict[str, object]] = []
    _fake_llm(monkeypatch, iter([_finish_action()]), payloads)
    HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=1,
        context_compression_mode="acp",
    )
    assert payloads[0]["acp_nudge"]["level"] == "normal"
    assert "非强制" in payloads[0]["acp_nudge"]["message"]

    legacy_payloads: list[dict[str, object]] = []
    _fake_llm(monkeypatch, iter([_finish_action()]), legacy_payloads)
    HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=1,
    )
    assert "acp_nudge" not in legacy_payloads[0]


def test_task_payload_no_nudge_below_threshold(monkeypatch) -> None:
    monkeypatch.setattr(
        harness_agent_module, "latest_llm_usage_observation", lambda _session_id=None: LOW_USAGE
    )
    payloads: list[dict[str, object]] = []
    _fake_llm(monkeypatch, iter([_finish_action()]), payloads)
    HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=1,
        context_compression_mode="acp",
    )
    assert "acp_nudge" not in payloads[0]


def test_task_payload_emergency_nudge_escalates(monkeypatch) -> None:
    monkeypatch.setattr(
        harness_agent_module, "latest_llm_usage_observation", lambda _session_id=None: EMERGENCY_USAGE
    )
    payloads: list[dict[str, object]] = []
    _fake_llm(monkeypatch, iter([_finish_action()]), payloads)
    HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=1,
        context_compression_mode="acp",
    )
    assert payloads[0]["acp_nudge"]["level"] == "emergency"
    assert "紧急" in payloads[0]["acp_nudge"]["message"]


def test_task_payload_nudge_flagged_estimated_when_usage_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        harness_agent_module, "latest_llm_usage_observation", lambda _session_id=None: None
    )
    payloads: list[dict[str, object]] = []
    _fake_llm(monkeypatch, iter([_finish_action()]), payloads)
    HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=1,
        context_compression_mode="acp",
    )
    assert "acp_nudge" not in payloads[0]


def test_task_payload_nudge_does_not_block_finish(monkeypatch) -> None:
    """autoNudge semantics: the nudge is advisory and never hard-blocks."""
    monkeypatch.setattr(
        harness_agent_module, "latest_llm_usage_observation", lambda _session_id=None: EMERGENCY_USAGE
    )
    payloads: list[dict[str, object]] = []
    _fake_llm(monkeypatch, iter([_finish_action()]), payloads)
    result = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=1,
        context_compression_mode="acp",
    )
    assert result.status == "completed"
    assert payloads[0]["acp_nudge"]["level"] == "emergency"