import pytest

from app.llm.client import LLMClient, LLMError, _thinking_mode_for_model
from app.llm.protocol_drivers import ChatCompletionsDriver
from app.llm.output_policy import operation_output_tokens
from app.llm.stage_protocol import TURN_STAGE_MESSAGES_KEY, stage_payload
from app.llm.schemas import ModelConfigCreateRequest
from app.observability.spans import bind_span_sink, llm_operation


class _ForbiddenResponses:
    def create(self, **_kwargs):  # noqa: ANN003
        raise AssertionError("responses.create must not be called for OpenAI-compatible models")


class _FakeChatCompletions:
    def __init__(self) -> None:
        self.calls = []

    def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        message = type("Message", (), {"content": "ok"})()
        choice = type("Choice", (), {"message": message})()
        return type("Completion", (), {"choices": [choice]})()


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeChatCompletions()


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = _ForbiddenResponses()
        self.chat = _FakeChat()


def test_llm_client_uses_600_second_timeout(monkeypatch):
    captured = {}

    def fake_decrypt_secret(_value):  # noqa: ANN001
        return "api-key"

    def fake_openai(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return _FakeOpenAIClient()

    settings = type("Settings", (), {"model_api_timeout_seconds": 600.0})()
    model_config = type(
        "ModelConfig",
        (),
        {
            "api_key_encrypted": "encrypted",
            "base_url": "https://example.test/v1",
            "model": "demo-model",
            "temperature": 0.2,
            "max_output_tokens": 256,
            "extra_body_json": {
                "thinking": {"type": "disabled"},
                "do_sample": False,
            },
        },
    )()
    monkeypatch.setattr("app.llm.client.decrypt_secret", fake_decrypt_secret)
    monkeypatch.setattr("app.llm.client.OpenAI", fake_openai)
    monkeypatch.setattr("app.llm.client.get_settings", lambda: settings)

    client = LLMClient(model_config)

    assert client.timeout_seconds == 600.0
    assert captured["timeout"] == 600.0
    assert captured["base_url"] == "https://example.test/v1"
    assert client.extra_body == {
        "thinking": {"type": "disabled"},
        "do_sample": False,
    }
    assert client.thinking_mode == "disabled"


def test_llm_client_preserves_custom_openai_base_url(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr("app.llm.client.decrypt_secret", lambda _value: "api-key")
    monkeypatch.setattr(
        "app.llm.client.OpenAI",
        lambda **kwargs: captured.update(kwargs) or _FakeOpenAIClient(),
    )
    monkeypatch.setattr(
        "app.llm.client.get_settings",
        lambda: type("Settings", (), {"model_api_timeout_seconds": 30.0})(),
    )
    config = type(
        "ModelConfig",
        (),
        {
            "api_key_encrypted": "encrypted",
            "base_url": "https://custom-relay.example/llm",
            "model": "custom-model",
            "temperature": 0.2,
            "max_output_tokens": 128,
            "extra_body_json": {},
        },
    )()

    LLMClient(config)

    assert captured["base_url"] == "https://custom-relay.example/llm"


def test_model_config_create_defaults_to_8192_output_tokens():
    request = ModelConfigCreateRequest(
        tenant_id="tenant_demo",
        name="demo",
        model="demo-model",
    )

    assert request.max_output_tokens == 8192
    assert request.extra_body == {}


def _completion_with_content(content):  # noqa: ANN001
    return type(
        "Completion",
        (),
        {
            "choices": [
                type(
                    "Choice",
                    (),
                    {"message": type("Message", (), {"content": content})()},
                )()
            ]
        },
    )()


def test_generate_text_uses_chat_completions_only():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    output = client.generate_text("system prompt", {"hello": "world"})

    assert output == "ok"
    call = client.client.chat.completions.calls[0]
    assert call["model"] == "demo-model"
    assert call["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": '{"hello": "world"}'},
    ]
    assert call["max_tokens"] == 256


def test_chat_completions_driver_preserves_non_stream_and_stream_requests():
    client = _FakeOpenAIClient()
    driver = ChatCompletionsDriver(client)
    request = {"model": "demo-model", "messages": [{"role": "user", "content": "hi"}]}

    assert driver.complete(request).choices[0].message.content == "ok"
    stream = driver.stream(request)

    assert client.chat.completions.calls[0] == request
    assert client.chat.completions.calls[1] == {**request, "stream": True}
    assert stream is not None


def test_generate_text_can_disable_provider_thinking():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    client.thinking_mode = "disabled"

    assert client.generate_text("system prompt", "hello") == "ok"

    call = client.client.chat.completions.calls[0]
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}


def test_generate_text_passes_model_extra_body_and_preserves_thinking_options():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "glm-5.2"
    client.temperature = 0.2
    client.max_output_tokens = 256
    client.thinking_mode = "disabled"
    client.extra_body = {
        "thinking": {"type": "disabled", "clear_thinking": True},
        "do_sample": False,
    }

    assert client.generate_text("system prompt", "hello") == "ok"

    call = client.client.chat.completions.calls[0]
    assert call["extra_body"] == {
        "thinking": {"type": "disabled", "clear_thinking": True},
        "do_sample": False,
    }


def test_thinking_mode_can_be_scoped_to_specific_models():
    assert _thinking_mode_for_model("disabled", "glm-5.2", "glm-5.2") == "disabled"
    assert _thinking_mode_for_model("disabled", "glm-5.2", "deepseek-v4-pro") == ""


def test_generate_text_preserves_plain_user_content_without_json_encoding():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    content = "技能标题：新SOP\n原始流程：\n收集报销事由并提交审批。"

    assert client.generate_text("system prompt", content) == "ok"
    assert client.client.chat.completions.calls[0]["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": content},
    ]


def test_generate_text_persists_provider_request_metrics():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    events: list[tuple[str, dict]] = []

    with bind_span_sink(lambda event_type, payload: events.append((event_type, payload))):
        with llm_operation("router.scene"):
            assert client.generate_text("system prompt", {"hello": "world"}) == "ok"

    assert [event_type for event_type, _ in events] == [
        "llm_call_started",
        "llm_call_finished",
    ]
    started, finished = events[0][1], events[1][1]
    assert started["span_id"] == finished["span_id"]
    assert finished["operation"] == "router.scene"
    assert finished["model"] == "demo-model"
    assert finished["attempt"] == 1
    assert finished["retry_count"] == 0
    assert finished["output_chars"] == 2
    assert finished["duration_ms"] >= 0
    assert finished["ttft_ms"] >= 0
    assert finished["system_prompt_chars"] == len("system prompt")
    assert finished["context_message_count"] == 0
    assert finished["context_text_chars"] == 0
    assert finished["payload_chars"] == len('{"hello": "world"}')
    assert finished["request_text_chars"] == len("system prompt") + len(
        '{"hello": "world"}'
    )
    assert finished["request_message_roles"] == ["system", "user"]
    assert len(finished["request_prefix_fingerprints"]) == 2


def test_generate_text_persists_provider_cache_usage_metrics():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    events: list[tuple[str, dict]] = []
    message = type("Message", (), {"content": "ok"})()
    choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
    prompt_details = type("PromptDetails", (), {"cached_tokens": 448})()
    usage = type(
        "Usage",
        (),
        {
            "prompt_tokens": 3217,
            "completion_tokens": 848,
            "total_tokens": 4065,
            "prompt_tokens_details": prompt_details,
        },
    )()
    completion = type(
        "Completion",
        (),
        {"id": "as-demo", "choices": [choice], "usage": usage},
    )()
    client.client.chat.completions.create = lambda **_kwargs: completion

    with bind_span_sink(lambda event_type, payload: events.append((event_type, payload))):
        assert client.generate_text("system", {"hello": "world"}) == "ok"

    finished = next(
        payload for event_type, payload in events if event_type == "llm_call_finished"
    )
    assert finished["input_tokens"] == 3217
    assert finished["cached_input_tokens"] == 448
    assert finished["uncached_input_tokens"] == 2769


def test_generate_text_retries_empty_response():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    contents = iter(["", None, "ok"])

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        return _completion_with_content(next(contents))

    client.client.chat.completions.create = fake_create

    assert client.generate_text("system prompt", {"hello": "world"}) == "ok"
    assert len(client.client.chat.completions.calls) == 3


def test_generate_text_records_each_empty_response_retry():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    contents = iter(["", None, "ok"])
    events: list[tuple[str, dict]] = []

    client.client.chat.completions.create = lambda **_kwargs: _completion_with_content(
        next(contents)
    )
    with bind_span_sink(lambda event_type, payload: events.append((event_type, payload))):
        assert client.generate_text("system prompt", {"hello": "world"}) == "ok"

    finished = [payload for event_type, payload in events if event_type == "llm_call_finished"]
    assert [item["status"] for item in finished] == ["empty", "empty", "success"]
    assert [item["attempt"] for item in finished] == [1, 2, 3]
    assert [item["retry_count"] for item in finished] == [0, 1, 2]


def test_generate_text_empty_response_reports_provider_diagnostics():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://user:secret@example.test/v1?token=hidden"
    client.timeout_seconds = 600.0
    client.temperature = 0.2
    client.max_output_tokens = 256

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        message = type(
            "Message",
            (),
            {
                "content": None,
                "reasoning_content": "provider-side reasoning",
                "refusal": None,
                "tool_calls": [],
            },
        )()
        choice = type("Choice", (), {"message": message, "finish_reason": "length"})()
        usage = type("Usage", (), {"completion_tokens": 256})()
        return type("Completion", (), {"id": "resp_demo", "choices": [choice], "usage": usage})()

    client.client.chat.completions.create = fake_create

    with pytest.raises(LLMError) as error:
        client.generate_text("system prompt", {"hello": "world"})

    detail = str(error.value)
    assert "Model returned an empty response after 3 attempts" in detail
    assert "provider returned no usable message.content" in detail
    assert "model=demo-model" in detail
    assert "endpoint=https://example.test/v1" in detail
    assert "finish_reason=length" in detail
    assert "reasoning_chars=23" in detail
    assert "completion_tokens=256" in detail
    assert "secret" not in detail
    assert "hidden" not in detail


def test_generate_text_reads_text_from_structured_content_parts():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    part = type("ContentPart", (), {"text": "structured answer"})()

    client.client.chat.completions.create = lambda **_kwargs: _completion_with_content([part])

    assert client.generate_text("system prompt", {"hello": "world"}) == "structured answer"


def test_generate_text_stream_reports_empty_stream_diagnostics():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.timeout_seconds = 600.0
    client.temperature = 0.2
    client.max_output_tokens = 256

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        delta = type("Delta", (), {"content": None, "reasoning_content": "reasoning only"})()
        choice = type("Choice", (), {"delta": delta, "finish_reason": "stop"})()
        chunk = type("Chunk", (), {"id": "chunk_demo", "choices": [choice]})()
        return iter([chunk])

    client.client.chat.completions.create = fake_create

    with pytest.raises(LLMError) as error:
        list(client.generate_text_stream("system prompt", {"hello": "world"}))

    detail = str(error.value)
    assert "stream_chunks=1" in detail
    assert "finish_reason=stop" in detail
    assert "reasoning_chars=14" in detail
    assert len(client.client.chat.completions.calls) == 3
    assert all(call["messages"][0] == {"role": "system", "content": "system prompt"} for call in client.client.chat.completions.calls)


def test_generate_text_stream_records_ttft_and_output_volume():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    events: list[tuple[str, dict]] = []

    def chunk(content, finish_reason=None):  # noqa: ANN001
        delta = type("Delta", (), {"content": content, "reasoning_content": None})()
        choice = type("Choice", (), {"delta": delta, "finish_reason": finish_reason})()
        return type("Chunk", (), {"id": "chunk_demo", "choices": [choice]})()

    client.client.chat.completions.create = lambda **_kwargs: iter(
        [chunk("你"), chunk("好", "stop")]
    )

    with bind_span_sink(lambda event_type, payload: events.append((event_type, payload))):
        with llm_operation("response.generate_stream"):
            assert "".join(client.generate_text_stream("system", {"hello": "world"})) == "你好"

    finished = next(
        payload for event_type, payload in events if event_type == "llm_call_finished"
    )
    assert finished["operation"] == "response.generate_stream"
    assert finished["stream"] is True
    assert finished["ttft_ms"] is not None
    assert finished["output_chars"] == 2
    assert finished["stream_chunks"] == 2
    assert finished["finish_reasons"] == ["stop"]


def test_generate_text_projects_conversation_context_messages():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    output = client.generate_text(
        "system prompt",
        {
            "user_message": "买两个",
            "execution_state": {
                "active_skill_id": "purchase",
                "active_step_id": None,
                "slots": {},
                "pending_tasks": [],
            },
            "conversation_context": {
                "messages": [
                    {"role": "user", "content": "我是 hx，我要买 A2"},
                    {"role": "assistant", "content": "请问买几个？"},
                    {"role": "user", "content": "买两个"},
                ],
                "metadata": {"total_messages": 3},
            },
        },
    )

    assert output == "ok"
    call = client.client.chat.completions.calls[0]
    assert call["messages"][0] == {"role": "system", "content": "system prompt"}
    assert sum(message["role"] == "system" for message in call["messages"]) == 1
    assert call["messages"][1:4] == [
        {"role": "user", "content": "我是 hx，我要买 A2"},
        {"role": "assistant", "content": "请问买几个？"},
        {"role": "user", "content": "买两个"},
    ]
    current_input = call["messages"][-1]
    assert current_input["role"] == "user"
    assert current_input["content"].startswith("本轮输入（仅用于当前调用，不写入对话历史）：")
    assert '"execution_state": {"active_skill_id": "purchase"}' in current_input["content"]
    assert '"user_message"' not in current_input["content"]
    assert '"conversation_context"' not in current_input["content"]
    assert '"active_step_id"' not in current_input["content"]
    assert '"slots"' not in current_input["content"]
    assert '"pending_tasks"' not in current_input["content"]


def test_stage_input_uses_stable_history_and_puts_memory_time_and_question_first():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    payload = stage_payload(
        phase="Router",
        user_message="我想申请报销",
        conversation_context={
            "messages": [
                {"role": "user", "content": "历史的信息可以被总结为：\n用户是研发人员"},
                {"role": "user", "content": "近期的历史信息总结为：\n正在咨询差旅"},
                {"role": "assistant", "content": "请说明本次需求"},
                {"role": "user", "content": "我想申请报销"},
            ],
            "metadata": {"current_turn_time": "2026-07-13T20:30:00+08:00"},
        },
        memory_context=[{"content": "用户偏好简洁回复", "id": "memory_internal"}],
        instructions="只根据技能摘要路由。",
        stage_data={"available_skills": [{"skill_id": "travel", "name": "差旅报销"}]},
        output_contract={"decision": "start_new_task | answer_only"},
    )

    assert client.generate_text("stable unified system", payload) == "ok"

    messages = client.client.chat.completions.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": "stable unified system"}
    assert messages[1:4] == [
        {"role": "user", "content": "历史的信息可以被总结为：\n用户是研发人员"},
        {"role": "user", "content": "近期的历史信息总结为：\n正在咨询差旅"},
        {"role": "assistant", "content": "请说明本次需求"},
    ]
    current = messages[-1]["content"]
    assert current.startswith("用户记忆：\n- 用户偏好简洁回复\n\n本轮时间：")
    assert "本轮时间：\n2026-07-13T20:30:00+08:00" in current
    assert "本轮用户输入：\n我想申请报销" in current
    assert "当前阶段：\nRouter" in current
    assert "思考要求：" in current
    assert "保留完成当前阶段所需的简短思考" in current
    assert "available_skills" in current
    assert "memory_internal" not in current
    assert sum("我想申请报销" in str(message["content"]) for message in messages) == 1


def test_stage_requests_append_each_input_and_output_to_one_turn_context() -> None:
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192
    outputs = iter(
        [
            '{"decision":"answer_only","confidence":0.9}',
            '{"reply":"已处理","is_step_completed":true}',
        ]
    )

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        return _completion_with_content(next(outputs))

    client.client.chat.completions.create = fake_create
    stable_messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "请说明需求"},
        {"role": "user", "content": "查询报销规则"},
    ]
    context = {
        "messages": stable_messages.copy(),
        "metadata": {"current_turn_time": "2026-07-13T21:00:00+08:00"},
    }

    router_payload = stage_payload(
        phase="Router",
        user_message="查询报销规则",
        conversation_context=context,
        memory_context=[],
        instructions="选择处理路径。",
        stage_data={"available_skills": []},
        output_contract={"decision": "answer_only"},
    )
    step_payload = stage_payload(
        phase="Step Agent",
        user_message="查询报销规则",
        conversation_context=context,
        memory_context=[],
        instructions="执行当前步骤。",
        stage_data={"current_step": {"node_id": "start"}},
        output_contract={"reply": "string"},
    )

    assert client.generate_json("stable unified system", router_payload)["decision"] == "answer_only"
    assert client.generate_json("stable unified system", step_payload)["reply"] == "已处理"

    first_request = client.client.chat.completions.calls[0]["messages"]
    second_request = client.client.chat.completions.calls[1]["messages"]
    assert first_request[0] == second_request[0] == {
        "role": "system",
        "content": "stable unified system",
    }
    assert second_request[1:3] == stable_messages[:2]
    assert second_request[3] == first_request[-1]
    assert second_request[4] == {
        "role": "assistant",
        "content": '{"decision":"answer_only","confidence":0.9}',
    }
    assert second_request[5]["role"] == "user"
    assert "当前阶段：\nStep Agent" in second_request[5]["content"]
    assert "本轮用户输入：" not in second_request[5]["content"]
    assert sum(
        "本轮用户输入：" in str(message["content"])
        for message in second_request
    ) == 1
    assert context["messages"] == stable_messages
    assert context[TURN_STAGE_MESSAGES_KEY] == [
        first_request[-1],
        {"role": "assistant", "content": '{"decision":"answer_only","confidence":0.9}'},
        second_request[-1],
        {
            "role": "assistant",
            "content": '{"reply":"已处理","is_step_completed":true}',
        },
    ]


def test_stage_json_repair_continues_in_the_same_turn_context() -> None:
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192
    outputs = iter(["not json", '{"decision":"answer_only"}'])

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        return _completion_with_content(next(outputs))

    client.client.chat.completions.create = fake_create
    context = {
        "messages": [{"role": "user", "content": "你好"}],
        "metadata": {"current_turn_time": "2026-07-13T21:20:00+08:00"},
    }
    payload = stage_payload(
        phase="Router",
        user_message="你好",
        conversation_context=context,
        memory_context=[],
        instructions="输出路由 JSON。",
        stage_data={"available_skills": []},
        output_contract={"decision": "answer_only"},
    )

    assert client.generate_json("stable unified system", payload) == {
        "decision": "answer_only"
    }

    first_request = client.client.chat.completions.calls[0]["messages"]
    repair_request = client.client.chat.completions.calls[1]["messages"]
    assert repair_request[1] == first_request[-1]
    assert repair_request[2] == {"role": "assistant", "content": "not json"}
    assert repair_request[-1]["role"] == "user"
    assert '"_json_repair"' in repair_request[-1]["content"]
    assert "本轮用户输入：" not in repair_request[-1]["content"]
    assert context[TURN_STAGE_MESSAGES_KEY] == [
        first_request[-1],
        {"role": "assistant", "content": "not json"},
        repair_request[-1],
        {"role": "assistant", "content": '{"decision":"answer_only"}'},
    ]


def test_generate_text_keeps_append_only_history_prefix_for_kv_cache() -> None:
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    stable_history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好"},
        {"role": "user", "content": "查询退款规则"},
    ]

    client.generate_text(
        "stable system",
        {
            "conversation_context": {"messages": stable_history},
            "retrieved_knowledge": [{"label": "检索到的知识 1", "content": "七天内"}],
        },
    )
    client.generate_text(
        "stable system",
        {
            "conversation_context": {
                "messages": [
                    *stable_history,
                    {"role": "assistant", "content": "七天内可申请退款。"},
                    {"role": "user", "content": "需要什么材料？"},
                ]
            },
            "slots": {"topic": "退款材料"},
        },
    )

    first_messages = client.client.chat.completions.calls[0]["messages"]
    second_messages = client.client.chat.completions.calls[1]["messages"]
    assert first_messages[:4] == second_messages[:4]
    assert first_messages[0] == {"role": "system", "content": "stable system"}
    assert "检索到的知识 1" in first_messages[-1]["content"]
    assert "检索到的知识 1" not in str(second_messages)


def test_generate_text_projects_conversation_context_images_for_vision_model():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "gpt-4o-mini"
    client.temperature = 0.2
    client.max_output_tokens = 256

    output = client.generate_text(
        "system prompt",
        {
            "user_message": "看这张图",
            "conversation_context": {
                "messages": [
                    {
                        "role": "user",
                        "content": "看这张图",
                        "images": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,AAAA",
                                    "detail": "auto",
                                },
                            }
                        ],
                    }
                ],
            },
        },
    )

    assert output == "ok"
    call = client.client.chat.completions.calls[0]
    assert call["messages"][1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "auto"}},
        ],
    }
    assert '"messages":' not in call["messages"][-1]["content"]


def test_generate_text_keeps_memory_capture_history_as_role_messages() -> None:
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    assert client.generate_text(
        "memory prompt",
        {
            "conversation_context": {
                "messages": [
                    {"role": "user", "content": "我32岁"},
                    {"role": "assistant", "content": "已记录"},
                ]
            },
            "existing_memories": "- profile/age: 32",
        },
    ) == "ok"

    messages = client.client.chat.completions.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert sum(message["role"] == "system" for message in messages) == 1
    assert messages[0] == {"role": "system", "content": "memory prompt"}
    assert messages[1:3] == [
        {"role": "user", "content": "我32岁"},
        {"role": "assistant", "content": "已记录"},
    ]
    assert messages[-1]["role"] == "user"
    assert '"existing_memories": "- profile/age: 32"' in messages[-1]["content"]
    assert all('"conversation_context"' not in str(message["content"]) for message in messages)


def test_generate_text_does_not_guess_image_support_from_model_name():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "qwen3-6-27b"
    client.temperature = 0.2
    client.max_output_tokens = 256

    output = client.generate_text(
        "system prompt",
        {
            "conversation_context": {
                "messages": [
                    {
                        "role": "user",
                        "content": "看图",
                        "images": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}],
                    }
                ],
            },
        },
    )

    assert output == "ok"
    assert client.client.chat.completions.calls[0]["messages"][1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,AAAA"},
    }


def test_generate_json_extracts_fenced_json(monkeypatch):
    client = object.__new__(LLMClient)

    def fake_generate_text(_system_prompt, _payload):
        return '```json\n{"decision": "continue_active"}\n```'

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {}) == {"decision": "continue_active"}


def test_generate_json_requests_json_object_mode():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    client.client.chat.completions.create = lambda **kwargs: (  # noqa: E731
        client.client.chat.completions.calls.append(kwargs)
        or type(
            "Completion",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Message", (), {"content": '{"ok": true}'})()},
                    )()
                ]
            },
        )()
    )

    assert client.generate_json("prompt", {}) == {"ok": True}
    assert client.client.chat.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_internal_json_operation_caps_output_without_mutating_system_prompt():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192
    client.client.chat.completions.create = lambda **kwargs: (  # noqa: E731
        client.client.chat.completions.calls.append(kwargs)
        or _completion_with_content('{"decision":"answer_only"}')
    )

    with llm_operation("router.scene"):
        assert client.generate_json("router prompt", {}) == {"decision": "answer_only"}

    call = client.client.chat.completions.calls[0]
    assert call["max_tokens"] == 4096
    assert call["messages"][0]["content"] == "router prompt"


def test_internal_output_budget_never_increases_smaller_model_config():
    assert operation_output_tokens("router.scene", 256) == 256


def test_user_visible_response_caps_output_budget_at_4096():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192

    with llm_operation("response.generate"):
        assert client.generate_text("system prompt", {}) == "ok"

    assert client.client.chat.completions.calls[0]["max_tokens"] == 4096


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("router.scene", 4096),
        ("step_agent.run", 4096),
        ("step_agent.repair", 4096),
        ("response.generate", 4096),
        ("response.generate_stream", 4096),
        ("reflection.review", 2048),
        ("general_skill.select", 2048),
        ("general_skill.review", 2048),
        ("general_skill.reply", 2048),
        ("knowledge.document_route", 2048),
        ("knowledge.bucket_route", 512),
        ("memory.capture", 1024),
        ("session.title", 512),
    ],
)
def test_control_plane_operation_output_budgets(operation, expected):  # noqa: ANN001
    assert operation_output_tokens(operation, 8192) == expected


def test_step_agent_caps_output_budget_at_4096() -> None:
    assert operation_output_tokens("step_agent.run", 8192) == 4096
    assert operation_output_tokens("step_agent.repair", 8192) == 4096


def test_generate_json_falls_back_when_json_object_mode_is_unsupported():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        if "response_format" in kwargs:
            raise ValueError("Unsupported parameter: response_format")
        return type(
            "Completion",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Message", (), {"content": '{"ok": true}'})()},
                    )()
                ]
            },
        )()

    client.client.chat.completions.create = fake_create

    assert client.generate_json("prompt", {}) == {"ok": True}
    assert "response_format" in client.client.chat.completions.calls[0]
    assert "response_format" not in client.client.chat.completions.calls[1]


def test_generate_json_falls_back_when_json_object_mode_returns_empty():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        if "response_format" in kwargs:
            return _completion_with_content("")
        return _completion_with_content('{"ok": true}')

    client.client.chat.completions.create = fake_create

    assert client.generate_json("prompt", {}) == {"ok": True}
    assert all("response_format" in call for call in client.client.chat.completions.calls[:3])
    assert "response_format" not in client.client.chat.completions.calls[3]


def test_generate_json_retries_invalid_json(monkeypatch):
    client = object.__new__(LLMClient)
    calls = iter(["not json", '{"ok": true}'])

    def fake_generate_text(_system_prompt, _payload):
        return next(calls)

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {}) == {"ok": True}


def test_generate_json_retry_keeps_original_payload(monkeypatch):
    client = object.__new__(LLMClient)
    payloads = []
    calls = iter(["not json", '{"ok": true}'])

    def fake_generate_text(_system_prompt, payload):
        payloads.append(payload)
        return next(calls)

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {"query": "廊坊天气", "skill": {"slug": "weather-zh"}}) == {"ok": True}
    assert payloads[1]["query"] == "廊坊天气"
    assert payloads[1]["skill"]["slug"] == "weather-zh"
    assert payloads[1]["_json_repair"]["previous_output"] == "not json"


def test_generate_json_repairs_unescaped_string_quotes_without_retry(monkeypatch):
    client = object.__new__(LLMClient)
    payloads = []

    def fake_generate_text(_system_prompt, payload, response_format=None):  # noqa: ANN001, ARG001
        payloads.append(payload)
        return (
            '{"decision": "start_new_task", "target_skill_id": "purchase", '
            '"reason": "user_name 在 memory 中已明确为"hm"，不需要追问", '
            '"slot_hints": {"user_name": "hm"}}'
        )

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    result = client.generate_json("prompt", {"query": "我想买东西"})

    assert result == {
        "decision": "start_new_task",
        "target_skill_id": "purchase",
        "reason": 'user_name 在 memory 中已明确为"hm"，不需要追问',
        "slot_hints": {"user_name": "hm"},
    }
    assert len(payloads) == 1
    assert "_json_repair" not in payloads[0]


def test_generate_json_repairs_trailing_commas_and_string_newlines(monkeypatch):
    client = object.__new__(LLMClient)

    def fake_generate_text(_system_prompt, _payload, response_format=None):  # noqa: ANN001, ARG001
        return '{"ok": true, "reason": "第一行\n第二行",}'

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {}) == {"ok": True, "reason": "第一行\n第二行"}


def test_generate_json_allows_multiple_repair_attempts(monkeypatch):
    client = object.__new__(LLMClient)
    payloads = []
    calls = iter(["not json", '{"reason": "用户称呼为"', '{"ok": true}'])

    def fake_generate_text(_system_prompt, payload):
        payloads.append(payload)
        return next(calls)

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {"query": "你好"}) == {"ok": True}
    assert payloads[1]["_json_repair"]["attempt"] == 1
    assert payloads[2]["_json_repair"]["attempt"] == 2
    assert "parser_error" in payloads[2]["_json_repair"]
