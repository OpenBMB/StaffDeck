"""The DSH subprocess never talks to a provider: every model call goes through the bridge's
OpenAI-compatible gateway, which runs StaffDeck's own LLM client for the turn's ModelConfig."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from staffdeck_dsh.bridge.capability_mcp import ActivationRegistry
from staffdeck_dsh.bridge.model_gateway import CHAT_COMPLETIONS_PATH, ModelGateway


class _Chunk:
    def __init__(self, d):
        self._d = d

    def model_dump(self, mode="json", exclude_none=True):
        return self._d


class _Driver:
    request_kind = "chat.completions"

    def __init__(self, seen, chunks, fail=False):
        self.seen = seen
        self.chunks = chunks
        self.fail = fail

    def complete(self, request):
        self.seen.append(request)
        if self.fail:
            raise RuntimeError("provider down")
        return _Chunk({"id": "cmpl", "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})

    def stream(self, request):
        self.seen.append(request)
        if self.fail:
            raise RuntimeError("provider down")
        return iter(_Chunk(c) for c in self.chunks)


class _Client:
    """Stand-in for app.llm.client.LLMClient (only the surface the gateway uses)."""

    def __init__(self, driver, *, protocol="openai_chat_completions", thinking="disabled"):
        self.driver = driver
        self.api_protocol = protocol
        self.model = "qwen-x"
        self.model_config_name = "Demo Qwen"
        self.base_url = "https://gw.example/v1"
        self.temperature = 0.2
        self.thinking_mode = thinking
        self.extra_body = {"thinking": {"type": thinking}} if thinking else {}


def _app(registry, client):
    app = Starlette()
    ModelGateway(registry, client_factory=lambda mc: client).mount(app)
    return TestClient(app)


def _activation(registry, *, model_config=SimpleNamespace(id="m1", model="qwen-x", max_output_tokens=1024, base_url="https://gw.example/v1")):
    traces = []
    host = SimpleNamespace(model_config=model_config, trace=lambda ev, payload: traces.append((ev, payload)))
    act = registry.register(host, lambda tid: None)
    return act, traces


def test_requires_live_activation():
    reg = ActivationRegistry()
    c = _app(reg, _Client(_Driver([], [])))
    assert c.post(CHAT_COMPLETIONS_PATH, json={"messages": []}).status_code == 401
    assert c.post(CHAT_COMPLETIONS_PATH, json={"messages": []}, headers={"authorization": "Bearer nope"}).status_code == 404


def test_streaming_passes_provider_chunks_and_applies_model_config():
    reg = ActivationRegistry()
    act, traces = _activation(reg)
    seen = []
    chunks = [
        {"id": "c1", "choices": [{"delta": {"role": "assistant", "tool_calls": [{"id": "call_1", "index": 0, "type": "function", "function": {"name": "mcp__staffdeck__knowledge_search", "arguments": ""}}]}, "finish_reason": None}]},
        {"id": "c1", "choices": [{"delta": {"tool_calls": [{"id": None, "index": 0, "function": {"name": None, "arguments": "{\"query\":\"年假\"}"}}]}, "finish_reason": None}]},
        {"id": "c1", "choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    c = _app(reg, _Client(_Driver(seen, chunks)))
    body = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}], "stream": True, "stream_options": {"include_usage": True},
            "reasoning_effort": "high", "thinking": {"type": "enabled"}, "max_tokens": 256000, "tools": [{"type": "function", "function": {"name": "mcp__staffdeck__knowledge_search", "parameters": {}}}]}
    with c.stream("POST", CHAT_COMPLETIONS_PATH, json=body, headers={"authorization": f"Bearer {act.token}"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        lines = [ln for ln in r.iter_lines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    payloads = [json.loads(ln[6:]) for ln in lines[:-1]]
    assert payloads[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "mcp__staffdeck__knowledge_search"
    assert payloads[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] is None  # provider shape untouched
    # what the provider actually received: the ModelConfig wins over the subprocess
    wire = seen[0]
    assert wire["model"] == "qwen-x" and wire["max_tokens"] == 1024 and wire["temperature"] == 0.2 and "stream" not in wire  # the driver adds stream=True itself
    assert wire["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in wire and "thinking" not in wire
    assert wire["tools"] and wire["stream_options"] == {"include_usage": True}
    events = [e for e, _ in traces]
    assert events == ["llm_call_started", "llm_call_finished"]
    finished = traces[-1][1]
    assert finished["engine"] == "dsh" and finished["input_tokens"] == 10 and finished["output_tokens"] == 5 and finished["finish_reason"] == "tool_calls"


def test_non_streaming_and_provider_failure():
    reg = ActivationRegistry()
    act, traces = _activation(reg)
    c = _app(reg, _Client(_Driver([], [])))
    r = c.post(CHAT_COMPLETIONS_PATH, json={"messages": [{"role": "user", "content": "hi"}], "stream": False}, headers={"authorization": f"Bearer {act.token}"})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hi"
    act2, traces2 = _activation(reg)
    c2 = _app(reg, _Client(_Driver([], [], fail=True)))
    r = c2.post(CHAT_COMPLETIONS_PATH, json={"messages": [{"role": "user", "content": "hi"}], "stream": True}, headers={"authorization": f"Bearer {act2.token}"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "PROVIDER_ERROR"
    assert [e for e, _ in traces2] == ["llm_call_started", "llm_call_failed"]


def test_unsupported_protocol_is_refused_with_guidance():
    reg = ActivationRegistry()
    act, _ = _activation(reg)
    c = _app(reg, _Client(_Driver([], []), protocol="anthropic_messages"))
    r = c.post(CHAT_COMPLETIONS_PATH, json={"messages": [{"role": "user", "content": "hi"}]}, headers={"authorization": f"Bearer {act.token}"})
    assert r.status_code == 400 and "Harness v2" in r.json()["error"]["message"]


def test_missing_model_config_is_operator_readable():
    reg = ActivationRegistry()
    act, _ = _activation(reg, model_config=None)
    c = _app(reg, _Client(_Driver([], [])))
    r = c.post(CHAT_COMPLETIONS_PATH, json={"messages": []}, headers={"authorization": f"Bearer {act.token}"})
    assert r.status_code == 409 and "模型配置" in r.json()["error"]["message"]


@pytest.mark.parametrize("requested,cap,expected", [(256000, 1024, 1024), (None, 1024, 1024), (500, 0, 500), (100, 1024, None)])
def test_max_tokens_is_bounded_by_model_config(requested, cap, expected):
    client = _Client(_Driver([], []))
    body = {"messages": [], **({"max_tokens": requested} if requested is not None else {})}
    wire = ModelGateway._wire_request(client, SimpleNamespace(max_output_tokens=cap), body)
    assert wire.get("max_tokens") == expected
