from __future__ import annotations

from types import SimpleNamespace

from app.llm.client import LLMClient
from app.llm.model_protocols import ModelApiProtocol
from app.llm.protocol_drivers import OpenAIResponsesDriver


class _Responses:
    def __init__(self, response, events=None) -> None:
        self.calls = []
        self.response = response
        self.events = events or []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.events if kwargs.get("stream") else self.response


class _ClosableEvents(list):
    def __init__(self, values) -> None:
        super().__init__(values)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _response(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_123",
        output_text=text,
        status="completed",
        usage=SimpleNamespace(input_tokens=4, output_tokens=2, total_tokens=6),
    )


def test_responses_driver_maps_messages_json_and_storage() -> None:
    responses = _Responses(_response("hello"))
    driver = OpenAIResponsesDriver(SimpleNamespace(responses=responses))

    result = driver.complete(
        {
            "model": "gpt-test",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "previous"},
            ],
            "temperature": 0.2,
            "max_tokens": 128,
            "response_format": {"type": "json_object"},
            "protocol_options": {"store": False},
        }
    )

    assert result.choices[0].message.content == "hello"
    assert result.usage.total_tokens == 6
    assert responses.calls[0] == {
        "model": "gpt-test",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "previous"}]},
        ],
        "temperature": 0.2,
        "max_output_tokens": 128,
        "instructions": "system",
        "text": {"format": {"type": "json_object"}},
        "store": False,
        "stream": False,
    }


def test_responses_driver_maps_stream_events_and_closes_stream() -> None:
    response = _response()
    events = _ClosableEvents(
        [
            SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_stream")),
            SimpleNamespace(type="response.output_text.delta", delta="hi"),
            SimpleNamespace(type="response.completed", response=response),
        ]
    )
    responses = _Responses(response, events)
    driver = OpenAIResponsesDriver(SimpleNamespace(responses=responses))

    chunks = list(
        driver.stream(
            {
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.2,
                "max_tokens": 128,
            }
        )
    )

    assert chunks[0].id == "resp_stream"
    assert chunks[0].choices[0].delta.content == "hi"
    assert chunks[1].choices[0].finish_reason == "completed"
    assert chunks[1].usage.total_tokens == 6
    assert responses.calls[0]["stream"] is True
    assert events.closed is True


def test_llm_client_uses_responses_driver_and_storage_option() -> None:
    client = object.__new__(LLMClient)
    client.api_protocol = ModelApiProtocol.OPENAI_RESPONSES
    client.client = SimpleNamespace(responses=_Responses(_response()))
    client.model = "gpt-test"
    client.temperature = 0.2
    client.max_output_tokens = 128
    client.protocol_options = {"store": False}

    assert client.generate_text("system", {"hello": "world"}) == "ok"
    call = client.client.responses.calls[0]
    assert call["store"] is False
    assert call["max_output_tokens"] == 128


def test_responses_json_uses_prompt_instead_of_text_format(monkeypatch) -> None:
    client = object.__new__(LLMClient)
    client.api_protocol = ModelApiProtocol.OPENAI_RESPONSES
    client.protocol_options = {"json_mode": "prompt"}
    calls = []

    def fake_generate_text(system_prompt, payload, response_format=None):  # noqa: ANN001
        calls.append((system_prompt, payload, response_format))
        return '{"ok": true}'

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("system", {"task": "json"}) == {"ok": True}
    assert "只返回一个合法 JSON object" in calls[0][0]
    assert calls[0][2] is None


def test_legacy_responses_json_defaults_to_prompt_mode(monkeypatch) -> None:
    client = object.__new__(LLMClient)
    client.api_protocol = ModelApiProtocol.OPENAI_RESPONSES
    client.protocol_options = {"store": False}
    calls = []

    def fake_generate_text(system_prompt, payload, response_format=None):  # noqa: ANN001
        calls.append((system_prompt, payload, response_format))
        return '{"ok": true}'

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("system", {"task": "json"}) == {"ok": True}
    assert "只返回一个合法 JSON object" in calls[0][0]
    assert calls[0][2] is None
