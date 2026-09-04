"""Tool-aware protocol adapters: every provider wire round-trips into OpenAI chat-completion dicts."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from staffdeck_harness.bridge import protocol_adapters as pa
from staffdeck_harness.bridge.protocol_adapters import (
    AnthropicAdapter,
    GeminiAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    adapter_for,
    anthropic_payload,
    gemini_payload,
    responses_payload,
)

PNG = "data:image/png;base64,iVBORw0KGgo="
TOOLS = [{"type": "function", "function": {
    "name": "knowledge_search", "description": "search",
    "parameters": {"type": "object", "properties": {"query": {"type": "string", "default": ""}},
                   "required": ["query"], "additionalProperties": False, "$schema": "x"},
}}]
CONVO = [
    {"role": "system", "content": "be terse"},
    {"role": "user", "content": [{"type": "text", "text": "find leave policy"},
                                 {"type": "image_url", "image_url": {"url": PNG}}]},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "knowledge_search", "arguments": "{\"query\": \"年假\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "{\"hits\": 2}"},
]


def _wire(**extra):
    return {"model": "m", "messages": CONVO, "temperature": 0.3, "max_tokens": 2048,
            "tools": TOOLS, "tool_choice": "auto", **extra}


def _client(protocol, sdk=None, driver=None):
    return NS(api_protocol=protocol, client=sdk, driver=driver, model="m",
              base_url="https://gen.example", api_key="k", temperature=0.3,
              max_output_tokens=2048, timeout_seconds=30, thinking_mode="", extra_body={})


def _collect(chunks):
    chunks = list(chunks)
    for c in chunks:
        assert c["object"] == "chat.completion.chunk" and c["model"] and c["id"]
        assert c["choices"][0]["index"] == 0
    text, args, calls, finish, usage = "", {}, {}, None, None
    for c in chunks:
        ch = c["choices"][0]
        for call in ch["delta"].get("tool_calls") or []:
            if call.get("id"):
                calls[call["index"]] = (call["id"], call["function"]["name"])
            args[call["index"]] = args.get(call["index"], "") + call["function"].get("arguments", "")
        text += ch["delta"].get("content") or ""
        if ch["finish_reason"]:
            finish = ch["finish_reason"]
        usage = c.get("usage") or usage
    assert chunks[-1]["choices"][0]["finish_reason"] == finish
    return NS(text=text, args=args, calls=calls, finish=finish, usage=usage, chunks=chunks)


# -- dispatch ---------------------------------------------------------------------------------


def test_adapter_for_dispatches_by_protocol_and_rejects_unknown():
    sdk = NS()
    assert isinstance(adapter_for(_client("openai_chat_completions", driver=NS())), OpenAIChatAdapter)
    assert isinstance(adapter_for(_client("openai_responses", sdk)), OpenAIResponsesAdapter)
    assert isinstance(adapter_for(_client("anthropic_messages", sdk)), AnthropicAdapter)
    assert isinstance(adapter_for(_client("gemini_generate_content", sdk)), GeminiAdapter)
    with pytest.raises(ValueError):
        adapter_for(_client("soap_rpc", sdk))


def test_openai_chat_adapter_delegates_to_driver_untouched():
    seen = []

    class Obj:
        def __init__(self, d):
            self.d = d

        def model_dump(self, mode="json", exclude_none=True):
            return self.d

    driver = NS(request_kind="chat.completions",
                complete=lambda w: (seen.append(w), Obj({"id": "x"}))[1],
                stream=lambda w: iter([Obj({"id": "a"}), {"id": "b"}]))
    adapter = adapter_for(_client("openai_chat_completions", driver=driver))
    wire = _wire()
    assert adapter.complete(wire) == {"id": "x"} and seen == [wire] and "stream" not in wire
    assert list(adapter.stream(wire)) == [{"id": "a"}, {"id": "b"}]
    assert adapter.request_kind == "chat.completions"


# -- anthropic ------------------------------------------------------------------------------


class _AnthropicSDK:
    def __init__(self, events=None, response=None):
        self.events, self.response, self.seen = events or [], response, []
        self.messages = NS(create=self._create)

    def _create(self, *, stream, **payload):
        self.seen.append(payload)
        return iter(self.events) if stream else self.response


def test_anthropic_request_translation():
    payload = anthropic_payload(_wire(stop=["END"]))
    assert payload["system"] == "be terse" and payload["max_tokens"] == 2048
    assert payload["temperature"] == 0.3 and payload["stop_sequences"] == ["END"]
    assert payload["messages"] == [
        {"role": "user", "content": [
            {"type": "text", "text": "find leave policy"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "knowledge_search", "input": {"query": "年假"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "{\"hits\": 2}"}]},
    ]
    assert payload["tools"] == [{"name": "knowledge_search", "description": "search",
                                 "input_schema": TOOLS[0]["function"]["parameters"]}]
    assert payload["tool_choice"] == {"type": "auto"}


@pytest.mark.parametrize("choice,expected", [
    ("required", {"type": "any"}), ("none", None),
    ({"type": "function", "function": {"name": "knowledge_search"}}, {"type": "tool", "name": "knowledge_search"}),
])
def test_anthropic_tool_choice(choice, expected):
    payload = anthropic_payload(_wire(tool_choice=choice))
    if expected is None:
        assert "tools" not in payload and "tool_choice" not in payload
    else:
        assert payload["tool_choice"] == expected


def test_anthropic_thinking_and_claude5_temperature_rules():
    p = anthropic_payload(_wire(extra_body={"thinking": {"type": "enabled"}}))
    assert p["thinking"] == {"type": "enabled", "budget_tokens": 1024} and "temperature" not in p
    p = anthropic_payload(_wire(model="claude-sonnet-5-20260101"))
    assert "temperature" not in p
    p = anthropic_payload({"model": "claude-3-7", "messages": [{"role": "assistant", "content": "x"}]})
    assert p["max_tokens"] == 4096 and p["messages"] == []  # leading assistant dropped


def test_anthropic_streamed_tool_call_round_trips():
    sdk = _AnthropicSDK(events=[
        NS(type="message_start", message=NS(id="msg_1", usage=NS(input_tokens=12, output_tokens=1))),
        NS(type="content_block_start", index=0, content_block=NS(type="text")),
        NS(type="content_block_delta", index=0, delta=NS(type="thinking_delta", thinking="hmm")),
        NS(type="content_block_delta", index=0, delta=NS(type="text_delta", text="Let me ")),
        NS(type="content_block_delta", index=0, delta=NS(type="text_delta", text="look.")),
        NS(type="content_block_stop", index=0),
        NS(type="content_block_start", index=1, content_block=NS(type="tool_use", id="toolu_9", name="knowledge_search")),
        NS(type="content_block_delta", index=1, delta=NS(type="input_json_delta", partial_json="{\"que")),
        NS(type="content_block_delta", index=1, delta=NS(type="input_json_delta", partial_json="ry\": \"年假\"}")),
        NS(type="content_block_stop", index=1),
        NS(type="message_delta", delta=NS(stop_reason="tool_use"), usage=NS(output_tokens=30)),
        NS(type="message_stop"),
    ])
    out = _collect(AnthropicAdapter(_client("anthropic_messages", sdk)).stream(_wire()))
    assert out.chunks[0]["id"] == "msg_1" and out.chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert out.text == "Let me look."
    assert out.calls == {0: ("toolu_9", "knowledge_search")}
    assert json.loads(out.args[0]) == {"query": "年假"}
    assert out.finish == "tool_calls"
    assert out.usage == {"prompt_tokens": 12, "completion_tokens": 30, "total_tokens": 42}
    assert any(c["choices"][0]["delta"].get("reasoning_content") == "hmm" for c in out.chunks)
    assert sdk.seen[0]["model"] == "m"


def test_anthropic_plain_text_stream_ends_with_stop_and_empty_tool_input_becomes_json():
    sdk = _AnthropicSDK(events=[
        NS(type="message_start", message=NS(id="msg_2", usage=NS(input_tokens=3, output_tokens=0))),
        NS(type="content_block_delta", index=0, delta=NS(type="text_delta", text="hi")),
        NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=1)),
    ])
    out = _collect(AnthropicAdapter(_client("anthropic_messages", sdk)).stream(_wire()))
    assert out.text == "hi" and out.finish == "stop" and out.calls == {}
    sdk = _AnthropicSDK(events=[
        NS(type="content_block_start", index=0, content_block=NS(type="tool_use", id="t", name="noop")),
        NS(type="content_block_stop", index=0),
        NS(type="message_delta", delta=NS(stop_reason="tool_use"), usage=NS(output_tokens=1)),
    ])
    out = _collect(AnthropicAdapter(_client("anthropic_messages", sdk)).stream(_wire()))
    assert out.args == {0: "{}"} and out.finish == "tool_calls"


def test_anthropic_complete_shape_and_error():
    sdk = _AnthropicSDK(response=NS(
        id="msg_3", stop_reason="tool_use", usage=NS(input_tokens=5, output_tokens=7),
        content=[NS(type="text", text="calling"),
                 NS(type="tool_use", id="toolu_1", name="knowledge_search", input={"query": "x"})]))
    data = AnthropicAdapter(_client("anthropic_messages", sdk)).complete(_wire())
    assert data["object"] == "chat.completion" and data["id"] == "msg_3"
    msg = data["choices"][0]["message"]
    assert msg["content"] == "calling" and msg["tool_calls"] == [
        {"id": "toolu_1", "type": "function", "function": {"name": "knowledge_search", "arguments": "{\"query\": \"x\"}"}}]
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    assert data["usage"] == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
    sdk = _AnthropicSDK(events=[NS(type="error", error=NS(type="overloaded_error", message="busy"))])
    with pytest.raises(RuntimeError, match="busy"):
        list(AnthropicAdapter(_client("anthropic_messages", sdk)).stream(_wire()))


# -- openai responses -----------------------------------------------------------------------


class _ResponsesSDK:
    def __init__(self, events=None, response=None):
        self.events, self.response, self.seen = events or [], response, []
        self.responses = NS(create=self._create)

    def _create(self, *, stream=False, **payload):
        self.seen.append(payload)
        return iter(self.events) if stream else self.response


def test_responses_request_translation():
    payload = responses_payload(_wire(tool_choice={"type": "function", "function": {"name": "knowledge_search"}}))
    assert payload["input"] == [
        {"role": "system", "content": [{"type": "input_text", "text": "be terse"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "find leave policy"},
                                     {"type": "input_image", "image_url": PNG}]},
        {"type": "function_call", "call_id": "call_1", "name": "knowledge_search", "arguments": "{\"query\": \"年假\"}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "{\"hits\": 2}"},
    ]
    assert payload["tools"] == [{"type": "function", "name": "knowledge_search", "description": "search",
                                 "parameters": TOOLS[0]["function"]["parameters"], "strict": False}]
    assert payload["tool_choice"] == {"type": "function", "name": "knowledge_search"}
    assert payload["max_output_tokens"] == 2048 and payload["store"] is False
    assert payload["temperature"] == 0.3 and "reasoning" not in payload
    assert responses_payload(_wire(tool_choice="required"))["tool_choice"] == "required"


def test_responses_reasoning_only_for_reasoning_models():
    thinking = {"thinking": {"type": "enabled"}}
    p = responses_payload(_wire(model="gpt-5-mini", extra_body=thinking))
    assert p["reasoning"] == {"effort": "medium"} and "temperature" not in p
    p = responses_payload(_wire(model="gpt-4.1", extra_body=thinking))
    assert "reasoning" not in p and p["temperature"] == 0.3


def test_responses_streamed_tool_call_round_trips():
    sdk = _ResponsesSDK(events=[
        NS(type="response.created", response=NS(id="resp_1")),
        NS(type="response.output_item.added", output_index=0, item=NS(type="reasoning", id="rs_1")),
        NS(type="response.reasoning_summary_text.delta", item_id="rs_1", delta="thinking"),
        NS(type="response.output_item.added", output_index=1,
           item=NS(type="function_call", id="fc_1", call_id="call_9", name="knowledge_search")),
        NS(type="response.function_call_arguments.delta", item_id="fc_1", output_index=1, delta="{\"query\":"),
        NS(type="response.function_call_arguments.delta", item_id="fc_1", output_index=1, delta=" \"年假\"}"),
        NS(type="response.function_call_arguments.done", item_id="fc_1", output_index=1, arguments="{\"query\": \"年假\"}"),
        NS(type="response.completed", response=NS(id="resp_1", status="completed",
                                                    usage=NS(input_tokens=10, output_tokens=4, total_tokens=14))),
    ])
    out = _collect(OpenAIResponsesAdapter(_client("openai_responses", sdk)).stream(_wire()))
    assert out.chunks[0]["id"] == "resp_1"
    assert out.calls == {0: ("call_9", "knowledge_search")} and json.loads(out.args[0]) == {"query": "年假"}
    assert out.finish == "tool_calls"
    assert out.usage == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
    assert any(c["choices"][0]["delta"].get("reasoning_content") == "thinking" for c in out.chunks)
    assert sdk.seen[0]["store"] is False and "stream" not in sdk.seen[0]


def test_responses_plain_text_stream_length_and_failure():
    sdk = _ResponsesSDK(events=[
        NS(type="response.created", response=NS(id="resp_2")),
        NS(type="response.output_text.delta", delta="hel"),
        NS(type="response.output_text.delta", delta="lo"),
        NS(type="response.completed", response=NS(id="resp_2", status="completed", usage=NS(input_tokens=1, output_tokens=2, total_tokens=3))),
    ])
    out = _collect(OpenAIResponsesAdapter(_client("openai_responses", sdk)).stream(_wire()))
    assert out.text == "hello" and out.finish == "stop" and out.calls == {}
    sdk = _ResponsesSDK(events=[
        NS(type="response.output_text.delta", delta="x"),
        NS(type="response.incomplete", response=NS(id="r", status="incomplete", incomplete_details=NS(reason="max_output_tokens"), usage=None)),
    ])
    assert _collect(OpenAIResponsesAdapter(_client("openai_responses", sdk)).stream(_wire())).finish == "length"
    sdk = _ResponsesSDK(events=[NS(type="response.failed", response=NS(id="r", error=NS(code="server_error", message="boom")))])
    with pytest.raises(RuntimeError, match="boom"):
        list(OpenAIResponsesAdapter(_client("openai_responses", sdk)).stream(_wire()))


def test_responses_complete_shape():
    sdk = _ResponsesSDK(response=NS(
        id="resp_3", status="completed", usage=NS(input_tokens=2, output_tokens=3, total_tokens=5),
        output=[NS(type="message", content=[NS(type="output_text", text="ok")]),
                NS(type="function_call", id="fc", call_id="call_2", name="knowledge_search", arguments="{}")]))
    data = OpenAIResponsesAdapter(_client("openai_responses", sdk)).complete(_wire())
    msg = data["choices"][0]["message"]
    assert msg["content"] == "ok" and msg["tool_calls"][0] == {
        "id": "call_2", "type": "function", "function": {"name": "knowledge_search", "arguments": "{}"}}
    assert data["choices"][0]["finish_reason"] == "tool_calls" and data["usage"]["total_tokens"] == 5
    sdk = _ResponsesSDK(response=NS(id="r", status="completed", usage=None,
                                    output=[NS(type="message", content=[NS(type="output_text", text="hi")])]))
    data = OpenAIResponsesAdapter(_client("openai_responses", sdk)).complete(_wire())
    assert data["choices"][0]["message"] == {"role": "assistant", "content": "hi"}
    assert data["choices"][0]["finish_reason"] == "stop" and "usage" not in data


# -- gemini ---------------------------------------------------------------------------------


class _Resp:
    def __init__(self, body=None, lines=(), status=200):
        self.status_code, self._body, self._lines, self.text = status, body, list(lines), json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    def read(self):
        return b""

    def iter_lines(self):
        return iter(self._lines)


class _Ctx:
    def __init__(self, resp, log):
        self.resp, self.log = resp, log

    def __enter__(self):
        return self.resp

    def __exit__(self, *exc):
        self.log.append("closed")
        return False


class _HTTP:
    def __init__(self, body=None, lines=(), status=200):
        self.body, self.lines, self.status, self.seen, self.log = body, lines, status, [], []

    def post(self, url, *, headers, json):
        self.seen.append((url, headers, json))
        return _Resp(self.body, status=self.status)

    def stream(self, method, url, *, headers, json):
        self.seen.append((url, headers, json))
        return _Ctx(_Resp(self.body, self.lines, status=self.status), self.log)


def _sse(*bodies):
    for b in bodies:
        yield f"data: {json.dumps(b, ensure_ascii=False)}"
        yield ""


def test_gemini_request_translation_and_endpoint():
    http = _HTTP(body={"candidates": []})
    GeminiAdapter(_client("gemini_generate_content", http)).complete(
        _wire(model="gemini-2.5-pro", tool_choice={"type": "function", "function": {"name": "knowledge_search"}}))
    url, headers, payload = http.seen[0]
    assert url == "https://gen.example/v1beta/models/gemini-2.5-pro:generateContent"
    assert headers["x-goog-api-key"] == "k" and headers["authorization"] == "Bearer k"
    assert payload["systemInstruction"] == {"parts": [{"text": "be terse"}]}
    assert payload["contents"] == [
        {"role": "user", "parts": [{"text": "find leave policy"},
                                   {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}]},
        {"role": "model", "parts": [{"functionCall": {"name": "knowledge_search", "args": {"query": "年假"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "knowledge_search", "response": {"result": {"hits": 2}}}}]},
    ]
    assert payload["tools"] == [{"functionDeclarations": [{
        "name": "knowledge_search", "description": "search",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]}]
    assert payload["toolConfig"] == {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["knowledge_search"]}}
    assert payload["generationConfig"] == {"temperature": 0.3, "maxOutputTokens": 2048}
    assert gemini_payload(_wire(tool_choice="none"))["toolConfig"]["functionCallingConfig"] == {"mode": "NONE"}
    # non-JSON tool output is wrapped, unknown call ids fall back to the id as the name
    p = gemini_payload({"model": "g", "messages": [{"role": "tool", "tool_call_id": "zz", "content": "plain"}]})
    assert p["contents"][0]["parts"][0]["functionResponse"] == {"name": "zz", "response": {"result": {"text": "plain"}}}


def test_gemini_streamed_function_call_round_trips_and_echoes_signature():
    http = _HTTP(lines=_sse(
        {"responseId": "g1", "candidates": [{"content": {"role": "model", "parts": [{"text": "plan", "thought": True}, {"text": "Searching"}]}}]},
        {"responseId": "g1", "candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "knowledge_search", "args": {"query": "年假"}}, "thoughtSignature": "sig-abc"}]},
            "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 6, "totalTokenCount": 14}},
    ))
    adapter = GeminiAdapter(_client("gemini_generate_content", http))
    out = _collect(adapter.stream(_wire(model="gemini-3-pro")))
    assert http.seen[0][0] == "https://gen.example/v1beta/models/gemini-3-pro:streamGenerateContent?alt=sse"
    assert out.text == "Searching" and out.chunks[1]["id"] == "g1"
    assert all(c["model"] == "gemini-3-pro" for c in out.chunks)
    assert any(c["choices"][0]["delta"].get("reasoning_content") == "plan" for c in out.chunks)
    assert list(out.calls.values()) == [(out.calls[0][0], "knowledge_search")]
    call_id = out.calls[0][0]
    assert call_id.startswith("call_") and json.loads(out.args[0]) == {"query": "年假"}
    assert out.finish == "tool_calls" and out.usage == {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14}
    assert http.log == ["closed"]
    # next turn: the engine echoes the tool call back; the cached thoughtSignature rides along
    p = gemini_payload({"model": "g", "messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "knowledge_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": "[]"}]})
    assert p["contents"][1]["parts"][0]["thoughtSignature"] == "sig-abc"
    assert p["contents"][2]["parts"][0]["functionResponse"]["name"] == "knowledge_search"


def test_gemini_plain_text_stream_and_http_error():
    http = _HTTP(lines=_sse(
        {"candidates": [{"content": {"parts": [{"text": "hel"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "lo"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2}},
    ))
    out = _collect(GeminiAdapter(_client("gemini_generate_content", http)).stream(_wire()))
    assert out.text == "hello" and out.finish == "stop" and out.usage["total_tokens"] == 3
    http = _HTTP(lines=_sse({"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "MAX_TOKENS"}]}))
    assert _collect(GeminiAdapter(_client("gemini_generate_content", http)).stream(_wire())).finish == "length"
    http = _HTTP(body={"error": {"code": 400, "message": "bad schema", "status": "INVALID_ARGUMENT"}}, status=400)
    with pytest.raises(RuntimeError, match="bad schema"):
        GeminiAdapter(_client("gemini_generate_content", http)).stream(_wire())
    assert http.log == ["closed"]
    with pytest.raises(RuntimeError, match="bad schema"):
        GeminiAdapter(_client("gemini_generate_content", http)).complete(_wire())


def test_gemini_complete_shape():
    http = _HTTP(body={"responseId": "g2", "candidates": [{"content": {"parts": [
        {"text": "ok"}, {"functionCall": {"name": "knowledge_search", "args": {"query": "x"}}}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4, "totalTokenCount": 7}})
    data = GeminiAdapter(_client("gemini_generate_content", http)).complete(_wire())
    assert data["id"] == "g2" and data["object"] == "chat.completion"
    msg = data["choices"][0]["message"]
    assert msg["content"] == "ok" and msg["tool_calls"][0]["function"] == {"name": "knowledge_search", "arguments": "{\"query\": \"x\"}"}
    assert data["choices"][0]["finish_reason"] == "tool_calls" and data["usage"]["total_tokens"] == 7
    http = _HTTP(body={"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "SAFETY"}]})
    data = GeminiAdapter(_client("gemini_generate_content", http)).complete(_wire())
    assert data["choices"][0]["finish_reason"] == "content_filter" and data["choices"][0]["message"]["content"] == "hi"


def test_gemini_schema_scrub_handles_nullable_unions_and_empty_objects():
    assert pa._gemini_schema({"type": ["string", "null"], "default": 1, "items": {"$schema": "x", "type": "integer"}}) == {
        "type": "string", "nullable": True, "items": {"type": "integer"}}
    assert pa._gemini_parameters({"type": "object", "properties": {}}) is None
    assert pa._gemini_parameters({}) is None


# -- degradation ------------------------------------------------------------------------------


def test_unknown_content_degrades_to_text_without_raising():
    weird = [{"role": "user", "content": [{"type": "audio", "data": "..."}, {"type": "text", "text": "hi"}, 42]},
             {"role": "assistant", "content": [{"type": "text", "text": "yo"}], "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "not json"}}]},
             {"role": "tool", "tool_call_id": "c", "content": [{"type": "text", "text": "r"}]}]
    wire = {"model": "m", "messages": weird}
    assert anthropic_payload(wire)["messages"][0]["content"] == [{"type": "text", "text": "hi"}]
    assert anthropic_payload(wire)["messages"][1]["content"][1]["input"] == {"_raw": "not json"}
    assert responses_payload(wire)["input"][0]["content"] == [{"type": "input_text", "text": "hi"}]
    assert responses_payload(wire)["input"][3] == {"type": "function_call_output", "call_id": "c", "output": "r"}
    assert gemini_payload(wire)["contents"][0]["parts"] == [{"text": "hi"}]
