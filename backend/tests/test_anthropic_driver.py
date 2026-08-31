from __future__ import annotations

from types import SimpleNamespace

from anthropic import Anthropic
import httpx

from app.llm.client import LLMClient
from app.llm.model_protocols import ModelApiProtocol
from app.llm.protocol_drivers import AnthropicMessagesDriver, CancellationToken, ProtocolCallError


class _Messages:
    def __init__(self, response=None, events=None) -> None:  # noqa: ANN001
        self.calls = []
        self.response = response
        self.events = events or []

    def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return self.events if kwargs.get("stream") else self.response


class _ClosableEvents(list):
    def __init__(self, values) -> None:  # noqa: ANN001
        super().__init__(values)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_anthropic_driver_maps_system_roles_images_and_usage() -> None:
    response = SimpleNamespace(
        id="msg_123",
        content=[
            SimpleNamespace(type="thinking", thinking="hidden"),
            SimpleNamespace(type="text", text="hello"),
            SimpleNamespace(type="text", text=" world"),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=12, output_tokens=3),
    )
    messages = _Messages(response=response)
    driver = AnthropicMessagesDriver(SimpleNamespace(messages=messages))

    result = driver.complete(
        {
            "model": "claude-test",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
                {"role": "user", "content": [{"type": "text", "text": "again"}]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        }
                    ],
                },
            ],
            "temperature": 0.2,
            "max_tokens": 128,
        }
    )

    assert result.choices[0].message.content == "hello world"
    assert result.usage.total_tokens == 15
    call = messages.calls[0]
    assert call["stream"] is False
    assert call["system"] == "system"
    assert call["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "again"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "AAAA",
                    },
                },
            ],
        }
    ]


def test_anthropic_driver_omits_temperature_for_claude_five() -> None:
    response = SimpleNamespace(id="msg_5", content=[], stop_reason="end_turn", usage=None)
    messages = _Messages(response=response)
    driver = AnthropicMessagesDriver(SimpleNamespace(messages=messages))

    driver.complete(
        {
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.2,
            "max_tokens": 32,
        }
    )

    assert "temperature" not in messages.calls[0]


def test_anthropic_driver_preserves_provider_error_diagnostics() -> None:
    class ProviderError(Exception):
        status_code = 422
        request_id = "req_anthropic"
        body = {
            "error": {
                "type": "invalid_request_error",
                "message": "unknown model",
                "api_key": "must-not-leak",
            }
        }

    class FailingMessages:
        def create(self, **_kwargs):  # noqa: ANN003
            raise ProviderError("request rejected")

    driver = AnthropicMessagesDriver(SimpleNamespace(messages=FailingMessages()))

    try:
        driver.complete(
            {
                "model": "missing-model",
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.2,
                "max_tokens": 32,
            }
        )
    except ProtocolCallError as exc:
        assert exc.code == "MODEL_INVALID_REQUEST"
        assert exc.status_code == 422
        assert exc.provider_code == "invalid_request_error"
        assert exc.provider_message == "unknown model"
        assert exc.request_id == "req_anthropic"
        assert "must-not-leak" not in str(exc.upstream_body)
        assert "[redacted]" in str(exc.upstream_body)
    else:
        raise AssertionError("provider error unexpectedly succeeded")


def test_anthropic_driver_maps_stream_events_to_chat_chunks() -> None:
    events = _ClosableEvents([
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg_stream", usage=SimpleNamespace(input_tokens=4, output_tokens=0)
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="hidden"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="hi"),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(input_tokens=None, output_tokens=2),
        ),
    ])
    messages = _Messages(events=events)
    driver = AnthropicMessagesDriver(SimpleNamespace(messages=messages))

    chunks = list(
        driver.stream(
            {
                "model": "claude-test",
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.2,
                "max_tokens": 128,
            }
        )
    )

    assert chunks[1].choices[0].delta.content == "hi"
    assert chunks[2].choices[0].finish_reason == "end_turn"
    assert messages.calls[0]["stream"] is True
    assert events.closed is True


def test_anthropic_driver_rejects_remote_image_urls() -> None:
    driver = AnthropicMessagesDriver(SimpleNamespace(messages=_Messages()))

    try:
        driver.complete(
            {
                "model": "claude-test",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.test/image.png"},
                            }
                        ],
                    }
                ],
                "temperature": 0.2,
                "max_tokens": 128,
            }
        )
    except ValueError as exc:
        assert str(exc) == "MODEL_IMAGE_DATA_URL_INVALID"
    else:
        raise AssertionError("remote image URL unexpectedly accepted")


def test_anthropic_json_uses_prompt_instead_of_openai_response_format(monkeypatch) -> None:
    client = object.__new__(LLMClient)
    client.api_protocol = ModelApiProtocol.ANTHROPIC_MESSAGES
    calls = []

    def fake_generate_text(system_prompt, payload, response_format=None):  # noqa: ANN001
        calls.append((system_prompt, payload, response_format))
        return '{"ok": true}'

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("system", {"task": "json"}) == {"ok": True}
    assert "只返回一个合法 JSON object" in calls[0][0]
    assert calls[0][2] is None


def test_llm_client_builds_anthropic_sdk(monkeypatch) -> None:
    captured = {}

    def fake_anthropic(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return SimpleNamespace(messages=_Messages())

    monkeypatch.setattr("app.llm.client.decrypt_secret", lambda _value: "secret")
    monkeypatch.setattr("app.llm.client.Anthropic", fake_anthropic)
    config = SimpleNamespace(
        api_protocol="anthropic_messages",
        purpose="verification",
        api_key_encrypted="encrypted",
        base_url="https://api.anthropic.test",
        model="claude-test",
        temperature=0.2,
        max_output_tokens=128,
        protocol_options={},
        legacy_extra_body={},
    )

    client = LLMClient(config)

    assert isinstance(client.driver, AnthropicMessagesDriver)
    assert captured["base_url"] == "https://api.anthropic.test"
    assert captured["max_retries"] == 0


def test_llm_client_adapts_anthropic_v1_api_root_for_sdk(monkeypatch) -> None:
    captured = {}

    def fake_anthropic(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return SimpleNamespace(messages=_Messages())

    monkeypatch.setattr("app.llm.client.decrypt_secret", lambda _value: "secret")
    monkeypatch.setattr("app.llm.client.Anthropic", fake_anthropic)
    config = SimpleNamespace(
        api_protocol="anthropic_messages",
        purpose="verification",
        api_key_encrypted="encrypted",
        base_url="https://llm-center.modelbest.cn/llm/v1/",
        model="claude-opus-5",
        temperature=0.2,
        max_output_tokens=128,
        protocol_options={},
        legacy_extra_body={},
    )

    LLMClient(config)

    assert captured["base_url"] == "https://llm-center.modelbest.cn/llm"


def test_llm_client_adapts_anthropic_full_messages_endpoint(monkeypatch) -> None:
    captured = {}

    def fake_anthropic(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return SimpleNamespace(messages=_Messages())

    monkeypatch.setattr("app.llm.client.decrypt_secret", lambda _value: "secret")
    monkeypatch.setattr("app.llm.client.Anthropic", fake_anthropic)
    config = SimpleNamespace(
        api_protocol="anthropic_messages",
        purpose="verification",
        api_key_encrypted="encrypted",
        base_url="https://llm-center.modelbest.cn/llm/v1/messages",
        model="claude-opus-5",
        temperature=0.2,
        max_output_tokens=128,
        protocol_options={},
        legacy_extra_body={},
    )

    LLMClient(config)

    assert captured["base_url"] == "https://llm-center.modelbest.cn/llm"


def test_llm_client_sets_outbound_user_agent_for_anthropic(monkeypatch) -> None:
    from app.llm.client import _OUTBOUND_USER_AGENT

    captured = {}

    def fake_anthropic(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return SimpleNamespace(messages=_Messages())

    monkeypatch.setattr("app.llm.client.decrypt_secret", lambda _value: "secret")
    monkeypatch.setattr("app.llm.client.Anthropic", fake_anthropic)
    config = SimpleNamespace(
        api_protocol="anthropic_messages",
        purpose="verification",
        api_key_encrypted="encrypted",
        base_url="https://api.anthropic.test",
        model="claude-test",
        temperature=0.2,
        max_output_tokens=128,
        protocol_options={},
        legacy_extra_body={},
    )

    LLMClient(config)

    assert captured["default_headers"]["User-Agent"] == _OUTBOUND_USER_AGENT
    assert not any(key.startswith("X-Stainless") for key in captured["default_headers"])
    assert captured["base_url"] == "https://api.anthropic.test"


def test_locked_anthropic_sdk_uses_messages_wire_contract() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "id": "msg_contract",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "model": "claude-test",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    sdk = Anthropic(
        api_key="contract-test-key",
        base_url="http://127.0.0.1:8123",
        http_client=http_client,
        max_retries=0,
    )
    driver = AnthropicMessagesDriver(sdk)

    result = driver.complete(
        {
            "model": "claude-test",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            "temperature": 0.2,
            "max_tokens": 32,
        }
    )

    assert result.choices[0].message.content == "ok"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:8123/v1/messages"
    assert captured["headers"]["x-api-key"] == "contract-test-key"
    assert captured["headers"]["anthropic-version"]
    assert '"system":"system"' in captured["body"]
    assert '"stream":false' in captured["body"]


def test_anthropic_stream_closes_when_consumer_stops() -> None:
    events = _ClosableEvents(
        [
            SimpleNamespace(
                type="message_start",
                message=SimpleNamespace(id="msg", usage=None),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="first"),
            ),
        ]
    )
    token = CancellationToken()
    driver = AnthropicMessagesDriver(SimpleNamespace(messages=_Messages(events=events)))
    stream = driver.stream(
        {
            "model": "claude-test",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.2,
            "max_tokens": 32,
            "_cancellation": token,
        }
    )
    next(stream)
    token.cancel()
    try:
        next(stream)
    except ProtocolCallError as exc:
        assert exc.code == "MODEL_CANCELLED"
    else:
        raise AssertionError("cancelled stream unexpectedly produced another event")
    assert events.closed is True
