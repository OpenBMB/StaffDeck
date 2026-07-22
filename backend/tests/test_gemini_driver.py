from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

from app.llm.client import LLMClient
from app.llm.protocol_drivers import (
    CancellationToken,
    GeminiGenerateContentDriver,
    ProtocolCallError,
)


def test_gemini_driver_uses_llm_center_generate_content_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "responseId": "gemini-response",
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "ok"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 4,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 6,
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    driver = GeminiGenerateContentDriver(
        client,
        "https://llm-center.modelbest.cn/llm",
        "gemini-key",
        "gemini-2.5-flash",
    )

    result = driver.complete(
        {
            "model": "ignored-by-driver",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "previous"},
            ],
            "temperature": 0.2,
            "max_tokens": 64,
            "response_format": {"type": "json_object"},
        }
    )

    assert result.choices[0].message.content == "ok"
    assert result.usage.total_tokens == 6
    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://llm-center.modelbest.cn/llm/v1beta/models/gemini-2.5-flash:generateContent"
    )
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["x-goog-api-key"] == "gemini-key"
    assert headers["authorization"] == "Bearer gemini-key"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["systemInstruction"] == {"parts": [{"text": "system"}]}
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "model", "parts": [{"text": "previous"}]},
    ]
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_driver_maps_sse_stream_and_closes_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"responseId":"stream-1","candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\n\n'
                b'data: {"candidates":[{"finishReason":"STOP"}]}\n\n'
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    driver = GeminiGenerateContentDriver(
        client,
        "https://llm-center.modelbest.co/llm",
        "gemini-key",
        "gemini-test",
    )

    chunks = list(
        driver.stream(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.2,
                "max_tokens": 32,
            }
        )
    )

    assert captured["url"] == (
        "https://llm-center.modelbest.co/llm/v1beta/models/gemini-test:streamGenerateContent?alt=sse"
    )
    assert chunks[0].choices[0].delta.content == "hi"
    assert chunks[1].choices[0].finish_reason == "STOP"


def test_gemini_driver_maps_http_errors_and_cancellation() -> None:
    def error_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})

    client = httpx.Client(transport=httpx.MockTransport(error_handler))
    driver = GeminiGenerateContentDriver(client, "https://example.test/llm", "key", "model")
    try:
        driver.complete(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.2,
                "max_tokens": 32,
            }
        )
    except ProtocolCallError as exc:
        assert exc.code == "MODEL_RATE_LIMITED"
        assert exc.retryable is True
    else:
        raise AssertionError("rate limit unexpectedly succeeded")

    token = CancellationToken()
    token.cancel()
    try:
        driver.complete(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.2,
                "max_tokens": 32,
                "_cancellation": token,
            }
        )
    except ProtocolCallError as exc:
        assert exc.code == "MODEL_CANCELLED"
    else:
        raise AssertionError("cancelled request unexpectedly succeeded")


def test_llm_client_builds_gemini_driver(monkeypatch) -> None:
    http_client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    monkeypatch.setattr("app.llm.client.decrypt_secret", lambda _value: "secret")
    monkeypatch.setattr("app.llm.client.httpx.Client", lambda **_kwargs: http_client)
    config = SimpleNamespace(
        api_protocol="gemini_generate_content",
        purpose="verification",
        api_key_encrypted="encrypted",
        base_url="https://llm-center.modelbest.cn/llm",
        model="gemini-2.5-flash",
        temperature=0.2,
        max_output_tokens=128,
        protocol_options={},
        legacy_extra_body={},
    )

    client = LLMClient(config)

    assert isinstance(client.driver, GeminiGenerateContentDriver)
    assert client.driver.base_url == "https://llm-center.modelbest.cn/llm"
