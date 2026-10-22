"""对话响应优化（P0-1/P0-2/P1-1）的行为验证。

覆盖：
- P0-1 知识路由降级：HarnessCapabilityInvoker._search_knowledge 使用
  意图识别轻量模型；未配置时回退主模型。
- P0-2 JSON repair 收紧：harness.task_action / turn_planner.plan 操作
  只重试 1 次；其他操作保持默认 3 次。
- P1-1 轻量节点直通：轻量模型承担首轮 harness 决策；协议失败回退主模型。
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.engine.step_loop import (
    LIGHTWEIGHT_DIRECTIVE,
    HarnessTaskAgent,
)
from app.core.engine.invoke import HarnessCapabilityInvoker
from app.core.base.request_compiler import (
    CapabilityDescriptor,
    CapabilityManifest,
    TaskRequirement,
)
from app.db.models import ModelConfig
from app.observability.spans import llm_operation
from app.llm import client as llm_client_module
from app.llm.output_policy import (
    OPERATION_JSON_REPAIR_ATTEMPTS,
    OPERATION_THINKING_MODE,
    OPERATION_TIMEOUT_SECONDS,
    operation_json_repair_attempts,
    operation_thinking_mode,
    operation_timeout_seconds,
)
from app.llm.protocol_drivers import ChatCompletionsDriver


def _test_engine() -> any:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _model_config(name: str = "测试主模型", model: str = "test-model") -> ModelConfig:
    return ModelConfig(
        id=f"model-{model}",
        tenant_id="tenant-demo",
        name=name,
        api_key_encrypted="test",
        model=model,
    )


@pytest.fixture(autouse=True)
def _fake_llm_transport(monkeypatch):
    """绕过密钥解密与真实 SDK 构造；driver 打桩由各测试自行设置。"""
    monkeypatch.setattr(
        "app.llm.client.try_decrypt_secret", lambda _value: "api-key"
    )
    monkeypatch.setattr(
        "app.llm.client.get_settings",
        lambda: type("Settings", (), {"model_api_timeout_seconds": 60.0})(),
    )
    yield


class _NotJsonDriver:
    """每次返回非 JSON 文本，并记录请求。"""

    request_kind = "chat.completions"

    def __init__(self, calls: list):
        self._calls = calls

    def complete(self, request: dict) -> object:
        self._calls.append(request)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="这不是JSON"))]
        )


def _patch_client_driver(monkeypatch, calls: list) -> None:
    monkeypatch.setattr(
        llm_client_module, "OpenAI", lambda **_kwargs: SimpleNamespace()
    )
    monkeypatch.setattr(
        llm_client_module,
        "ChatCompletionsDriver",
        lambda _client: _NotJsonDriver(calls),
    )


def _task_requirement() -> TaskRequirement:
    descriptor = CapabilityDescriptor(
        capability_id="tool-1",
        name="tool.echo",
        kind="tool",
        metadata={"tool_id": "tool-1"},
    )
    return TaskRequirement(
        task_frame_id="task-light",
        kind="sop",
        goal="调用 echo 工具并完成",
        requirements=["调用 echo 工具"],
        required_capability_names=["tool.echo"],
        allowed_transitions=[
            {"next_node_id": "node-next", "condition": "", "label": "", "priority": ""}
        ],
        capability_manifest=CapabilityManifest(available=[descriptor]),
    )


# ---------------------------------------------------------------------------
# 交互式操作超时收紧（网关挂死时快速失败，而非阻塞整轮 600s）
# ---------------------------------------------------------------------------


def test_operation_timeout_policy() -> None:
    # 2026-09-28 现场：任务型对话末轮 payload 41K + 思考模式，90s 上限把「接近
    # 完成」的第 14 轮判死，故放宽到 120s（见 output_policy.OPERATION_TIMEOUT_SECONDS）。
    assert OPERATION_TIMEOUT_SECONDS["harness.task_action"] == 120.0
    assert OPERATION_TIMEOUT_SECONDS["turn_planner.plan"] == 90.0
    assert OPERATION_TIMEOUT_SECONDS["sop.slot_extraction"] == 30.0
    # 未配置的操作（生成类/流式回复）保持模型级默认
    assert operation_timeout_seconds("response.generate", 600.0) == 600.0
    assert operation_timeout_seconds(None, 600.0) == 600.0
    assert operation_timeout_seconds("harness.task_action", 600.0) == 120.0


def test_generate_text_sets_interactive_request_timeout(monkeypatch) -> None:
    """交互式操作的下发请求携带 _request_timeout（驱动层转 SDK per-request timeout）。"""
    calls: list[dict] = []
    _patch_client_driver(monkeypatch, calls)

    client = llm_client_module.LLMClient(_model_config())
    with pytest.raises(llm_client_module.LLMError):
        with llm_operation("harness.task_action"):
            client.generate_json("系统提示", {"task_requirement": {}})

    # 断言的是「按操作下发的超时被驱动层带下去了」，而不是某个写死的秒数——
    # 策略值调整（90 → 120）不应让这条用例失效。
    assert calls
    assert calls[0]["_request_timeout"] == operation_timeout_seconds(
        "harness.task_action", client.timeout_seconds
    )


def test_generate_text_keeps_default_timeout_for_generative(monkeypatch) -> None:
    """生成类操作不收紧：_request_timeout 等于模型级默认（桩配置为 60s）。"""
    calls: list[dict] = []
    _patch_client_driver(monkeypatch, calls)

    client = llm_client_module.LLMClient(_model_config())
    with pytest.raises(llm_client_module.LLMError):
        with llm_operation("response.generate"):
            client.generate_json("系统提示", {"x": 1})

    assert calls and calls[0]["_request_timeout"] == client.timeout_seconds == 60.0


def test_chat_completions_driver_passes_request_timeout() -> None:
    """驱动层把 _request_timeout 转为 SDK per-request timeout，且不进入 payload。"""
    captured: dict = {}

    class _FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    driver = ChatCompletionsDriver(
        client=SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    )
    request = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "_request_timeout": 30.0,
    }
    driver.complete(request)
    assert captured.get("timeout") == 30.0
    # 下划线键被剥离，不会透传给厂商
    assert "_request_timeout" not in captured

    captured.clear()
    driver.complete({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert "timeout" not in captured


# ---------------------------------------------------------------------------
# P0-2 JSON repair 收紧
# ---------------------------------------------------------------------------


def test_operation_json_repair_attempts_policy() -> None:
    assert OPERATION_JSON_REPAIR_ATTEMPTS["harness.task_action"] == 1
    assert OPERATION_JSON_REPAIR_ATTEMPTS["turn_planner.plan"] == 1
    assert OPERATION_JSON_REPAIR_ATTEMPTS["sop.slot_extraction"] == 0
    # 未配置的操作回落默认值
    assert operation_json_repair_attempts("response.generate", 3) == 3
    # 配置的操作返回收紧值
    assert operation_json_repair_attempts("harness.task_action", 3) == 1
    assert operation_json_repair_attempts("turn_planner.plan", 3) == 1
    assert operation_json_repair_attempts("sop.slot_extraction", 3) == 0


def test_generate_json_repair_attempts_respects_operation(monkeypatch) -> None:
    """harness 决策操作下 JSON 解析失败只重试 1 次（共 2 次调用）。"""
    calls: list[dict] = []
    _patch_client_driver(monkeypatch, calls)

    client = llm_client_module.LLMClient(_model_config())
    with pytest.raises(llm_client_module.LLMError):
        with llm_operation("harness.task_action"):
            client.generate_json("系统提示", {"task_requirement": {}})

    # harness.task_action 配置 repair=1：首次 + 1 次 repair = 2 次 LLM 调用
    assert len(calls) == 2
    # repair 请求带 _json_repair 指令
    repair_message = calls[-1]["messages"][-1]["content"]
    assert "_json_repair" in str(repair_message) or "合法 JSON" in str(repair_message)


def test_generate_json_repair_default_attempts_untouched(monkeypatch) -> None:
    """非收紧操作（如 response.generate）保持默认 3 次 repair（共 4 次调用）。"""
    calls: list[dict] = []
    _patch_client_driver(monkeypatch, calls)

    client = llm_client_module.LLMClient(_model_config())
    with pytest.raises(llm_client_module.LLMError):
        with llm_operation("response.generate"):
            client.generate_json("系统提示", {"x": 1})

    assert len(calls) == 4


# ---------------------------------------------------------------------------
# P1-1 轻量节点直通
# ---------------------------------------------------------------------------


def test_harness_task_agent_lightweight_first_iteration_falls_back_on_protocol_error() -> None:
    """轻量模型首轮决策协议失败 → 第二次尝试回退主模型，最终成功。"""
    decision_models: list[str] = []
    payloads: list[dict] = []

    def fake_generate(client, system_prompt, payload, preview_sink=None):
        decision_models.append(str(getattr(client, "model", "")))
        payloads.append(deepcopy(payload))
        call_index = len(decision_models)
        if call_index == 1:
            # 轻量模型输出坏协议
            return {"bogus": True}
        # 主模型输出合法 tool+finish 序列
        return {
            "actions": [
                {
                    "action": "tool",
                    "tool_name": "tool.echo",
                    "arguments": {"text": "hello"},
                },
                {
                    "action": "finish",
                    "status": "completed",
                    "reply_fragment": "完成。",
                    "next_step_id": "node-next",
                },
            ]
        }

    lightweight = _model_config("轻量模型", "glm-flash")
    main = _model_config("主模型", "main-model")

    agent = HarnessTaskAgent()
    original = HarnessTaskAgent.__module__
    import app.core.engine.step_loop as ha

    saved = ha._generate_harness_action_json
    ha._generate_harness_action_json = fake_generate
    try:
        result = agent.run(
            _task_requirement(),
            main,
            lambda name, arguments: {"success": True, "data": {"echo": arguments}},
            max_actions=6,
            lightweight_model_config=lightweight,
        )
    finally:
        ha._generate_harness_action_json = saved
    assert original  # 引用避免未使用告警

    # 首次尝试轻量模型 + 指令注入，失败后第二轮回退主模型
    assert decision_models[0] == "glm-flash"
    assert decision_models[1] == "main-model"
    # 轻量轮带执行指令；主模型回退轮不带
    assert payloads[0].get("execution_directive") == LIGHTWEIGHT_DIRECTIVE
    assert "execution_directive" not in payloads[1]

    assert result.status == "completed"
    assert result.reply_fragment == "完成。"
    assert result.next_step_id == "node-next"
    assert result.action_count == 2


def test_harness_task_agent_lightweight_success_avoids_main_model() -> None:
    """轻量模型首轮成功输出动作序列 → 主模型不再被调用。"""
    decision_models: list[str] = []
    payloads: list[dict] = []

    def fake_generate(client, system_prompt, payload, preview_sink=None):
        decision_models.append(str(getattr(client, "model", "")))
        payloads.append(deepcopy(payload))
        return {
            "actions": [
                {
                    "action": "tool",
                    "tool_name": "tool.echo",
                    "arguments": {"text": "hi"},
                },
                {
                    "action": "finish",
                    "status": "completed",
                    "reply_fragment": "轻量完成。",
                    "next_step_id": "node-next",
                },
            ]
        }

    lightweight = _model_config("轻量模型", "glm-flash")
    main = _model_config("主模型", "main-model")

    import app.core.engine.step_loop as ha

    saved = ha._generate_harness_action_json
    ha._generate_harness_action_json = fake_generate
    try:
        result = HarnessTaskAgent().run(
            _task_requirement(),
            main,
            lambda name, arguments: {"success": True, "data": {"echo": arguments}},
            max_actions=6,
            lightweight_model_config=lightweight,
        )
    finally:
        ha._generate_harness_action_json = saved

    assert decision_models == ["glm-flash"]
    assert payloads[0].get("execution_directive") == LIGHTWEIGHT_DIRECTIVE
    assert result.status == "completed"


def test_harness_task_agent_no_lightweight_keeps_main_model() -> None:
    """未提供轻量模型时行为与历史一致：决策只走主模型（无指令注入）。"""
    decision_models: list[str] = []
    payloads: list[dict] = []

    def fake_generate(client, system_prompt, payload, preview_sink=None):
        decision_models.append(str(getattr(client, "model", "")))
        payloads.append(deepcopy(payload))
        return {
            "actions": [
                {
                    "action": "tool",
                    "tool_name": "tool.echo",
                    "arguments": {"text": "hi"},
                },
                {
                    "action": "finish",
                    "status": "completed",
                    "reply_fragment": "直接完成。",
                },
            ]
        }

    main = _model_config("主模型", "main-model")

    import app.core.engine.step_loop as ha

    saved = ha._generate_harness_action_json
    ha._generate_harness_action_json = fake_generate
    try:
        result = HarnessTaskAgent().run(
            _task_requirement(),
            main,
            lambda name, arguments: {"success": True, "data": {"echo": arguments}},
            max_actions=6,
        )
    finally:
        ha._generate_harness_action_json = saved

    # 无轻量模型：所有决策都来自主模型，且任何轮都不注入执行指令
    assert decision_models and set(decision_models) == {"main-model"}
    assert all("execution_directive" not in payload for payload in payloads)
    assert result.status == "completed"


# ---------------------------------------------------------------------------
# P0-1 知识路由降级
# ---------------------------------------------------------------------------


class _RecordingKnowledgeService:
    """捕获 search 收到的 model_config，返回带 model_dump 的空结果。"""

    def __init__(self, capture: dict):
        self._capture = capture

    def search(self, request, model_config=None):
        self._capture["model_config"] = model_config
        return SimpleNamespace(
            model_dump=lambda mode="json": {
                "selected_buckets": [],
                "chunks": [],
                "trace": [],
                "route_trace": [],
                "selected_documents": [],
                "selected_concepts": [],
                "expanded_sections": [],
                "okf_citations": [],
                "evidence_pack": [],
            },
            citations=[],
        )


def _invoker(db, main_model, tenant_id: str = "tenant-demo") -> HarnessCapabilityInvoker:
    session = SimpleNamespace(
        id="session-1", tenant_id=tenant_id, agent_id=None, slots_json={}
    )
    return HarnessCapabilityInvoker(
        db,
        tenant_id=tenant_id,
        session=session,
        task_frame_id="task-kb",
        model_config=main_model,
        manifest=CapabilityManifest(),
        active_skill=None,
        active_step_id=None,
        agent_id=None,
    )


def test_knowledge_routing_uses_intent_model(monkeypatch) -> None:
    """配置了意图识别模型时，_search_knowledge 传给 KnowledgeService 的是轻量模型。"""
    engine = _test_engine()
    main_model = _model_config("主模型", "main-model")
    lightweight = _model_config("轻量模型", "glm-flash")
    lightweight.is_intent_recognition = True

    captured: dict = {}
    monkeypatch.setattr(
        "app.core.engine.invoke.KnowledgeService",
        lambda db, laya_router=None: _RecordingKnowledgeService(captured),
    )

    def fake_model_for_agent(db, tenant_id, agent_id, role="default", user_id=None):
        assert role == "intent_recognition"
        return lightweight

    monkeypatch.setattr(
        "app.agents.branching.model_for_agent", fake_model_for_agent
    )

    with Session(engine) as db:
        invoker = _invoker(db, main_model)
        result = invoker._search_knowledge(
            {"allowed_knowledge_base_ids": ["kb-1"]},
            {"query": "怎么退款"},
            call_id="call-1",
        )

    assert result["success"] is True
    assert captured["model_config"] is lightweight


def test_knowledge_routing_falls_back_to_main_model(monkeypatch) -> None:
    """未配置意图识别模型时回退主模型。"""
    engine = _test_engine()
    main_model = _model_config("主模型", "main-model")

    captured: dict = {}
    monkeypatch.setattr(
        "app.core.engine.invoke.KnowledgeService",
        lambda db, laya_router=None: _RecordingKnowledgeService(captured),
    )

    def fake_model_for_agent(db, tenant_id, agent_id, role="default", user_id=None):
        return None

    monkeypatch.setattr(
        "app.agents.branching.model_for_agent", fake_model_for_agent
    )

    with Session(engine) as db:
        invoker = _invoker(db, main_model)
        result = invoker._search_knowledge(
            {"allowed_knowledge_base_ids": ["kb-1"]},
            {"query": "怎么退款"},
            call_id="call-1",
        )

    assert result["success"] is True
    assert captured["model_config"] is main_model


# ---------------------------------------------------------------------------
# 知识路由关闭思维链（纯分类调用不该把 token 烧在思维链上）
# 现场：一轮 102.12s 的对话里模型等待 100.72s，其中 document_route 9.61s /
# bucket_route 41.78s —— 两者答案合计只有 103 个字符（各自一个 id 列表）。
# ---------------------------------------------------------------------------


def test_operation_thinking_mode_policy() -> None:
    # 知识路由强制关闭，且能覆盖模型配置里的 enabled
    assert OPERATION_THINKING_MODE["knowledge.document_route"] == "disabled"
    assert OPERATION_THINKING_MODE["knowledge.bucket_route"] == "disabled"
    assert operation_thinking_mode("knowledge.bucket_route", "enabled") == "disabled"
    assert operation_thinking_mode("knowledge.document_route", "provider_default") == "disabled"
    # 未配置覆盖的调用沿用模型/全局设置
    assert operation_thinking_mode("harness.task_action", "enabled") == "enabled"
    assert operation_thinking_mode("harness.task_action", "") == ""
    assert operation_thinking_mode(None, "enabled") == "enabled"


def test_knowledge_route_request_disables_provider_thinking(monkeypatch) -> None:
    """知识路由的下发请求必须带 thinking.type=disabled。"""
    calls: list[dict] = []
    _patch_client_driver(monkeypatch, calls)

    client = llm_client_module.LLMClient(_model_config())
    with pytest.raises(llm_client_module.LLMError):
        with llm_operation("knowledge.bucket_route"):
            client.generate_json("系统提示", {"buckets": []})

    assert calls
    assert calls[0]["extra_body"]["thinking"]["type"] == "disabled"


def test_knowledge_route_thinking_override_beats_model_config(monkeypatch) -> None:
    """模型配置显式开启思维链时，知识路由仍要关闭（覆盖优先于配置）。"""
    calls: list[dict] = []
    _patch_client_driver(monkeypatch, calls)

    config = _model_config()
    config.extra_body_json = {"thinking": {"type": "enabled"}}
    client = llm_client_module.LLMClient(config)
    assert client.thinking_mode == "enabled"

    with pytest.raises(llm_client_module.LLMError):
        with llm_operation("knowledge.document_route"):
            client.generate_json("系统提示", {"documents": []})

    assert calls[0]["extra_body"]["thinking"]["type"] == "disabled"


def test_task_action_keeps_configured_thinking(monkeypatch) -> None:
    """反证：交互式决策轮不注入 thinking 覆盖，保持模型配置原样。"""
    calls: list[dict] = []
    _patch_client_driver(monkeypatch, calls)

    client = llm_client_module.LLMClient(_model_config())
    with pytest.raises(llm_client_module.LLMError):
        with llm_operation("harness.task_action"):
            client.generate_json("系统提示", {"task_requirement": {}})

    assert calls
    assert "extra_body" not in calls[0] or "thinking" not in (calls[0].get("extra_body") or {})
