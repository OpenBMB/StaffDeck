from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.llm.client import LLMClient, LLMError


def _config(**overrides):
    base = {
        "api_protocol": "openai_chat_completions",
        "api_key_encrypted": "encrypted",
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "temperature": 0.2,
        "max_output_tokens": 1,
        "extra_body_json": {},
        "protocol_options": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _settings(monkeypatch) -> None:
    monkeypatch.setattr("app.llm.client.decrypt_secret", lambda _value: "api-key")
    monkeypatch.setattr(
        "app.llm.client.get_settings",
        lambda: SimpleNamespace(model_api_timeout_seconds=30.0),
    )


class _FakeOpenAIModel:
    def __init__(self, model_id: str, created: int) -> None:
        self.id = model_id
        self.created = created


class _FakeOpenAIModelsResource:
    def list(self):
        # Deliberately out of creation order, and mixed with non-chat models —
        # matches what a real OpenAI-compatible /v1/models catalog looks like.
        return [
            _FakeOpenAIModel("gpt-4o-mini", created=1_700_000_000),
            _FakeOpenAIModel("text-embedding-3-large", created=1_800_000_000),
            _FakeOpenAIModel("gpt-5.1", created=1_800_000_000),
            _FakeOpenAIModel("whisper-1", created=1_650_000_000),
            _FakeOpenAIModel("gpt-4-0314", created=1_600_000_000),
        ]


class _FakeOpenAIClientWithModels:
    def __init__(self, **_kwargs) -> None:
        self.models = _FakeOpenAIModelsResource()


def test_list_models_normalizes_openai_catalog(monkeypatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr("app.llm.client.OpenAI", _FakeOpenAIClientWithModels)

    result = LLMClient(_config()).list_models()

    # Newest first, and the embedding/whisper entries are gone — a picker for
    # chat models shouldn't force the user to scroll past either.
    assert result == [
        {"id": "gpt-5.1", "label": "gpt-5.1"},
        {"id": "gpt-4o-mini", "label": "gpt-4o-mini"},
        {"id": "gpt-4-0314", "label": "gpt-4-0314"},
    ]


class _FakeAnthropicModel:
    def __init__(self, model_id: str, display_name: str, created_at: datetime) -> None:
        self.id = model_id
        self.display_name = display_name
        self.created_at = created_at


class _FakeAnthropicModelsResource:
    def list(self):
        return [
            _FakeAnthropicModel(
                "claude-sonnet-4-5", "Claude Sonnet 4.5", datetime(2026, 1, 1, tzinfo=timezone.utc)
            ),
            _FakeAnthropicModel(
                "claude-opus-4-5", "Claude Opus 4.5", datetime(2026, 6, 1, tzinfo=timezone.utc)
            ),
        ]


class _FakeAnthropicClientWithModels:
    def __init__(self, **_kwargs) -> None:
        self.models = _FakeAnthropicModelsResource()


def test_list_models_normalizes_anthropic_catalog(monkeypatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr("app.llm.client.Anthropic", _FakeAnthropicClientWithModels)

    result = LLMClient(_config(api_protocol="anthropic_messages")).list_models()

    # The newer release (Opus, June) sorts ahead of the older one (Sonnet, January).
    assert result == [
        {"id": "claude-opus-4-5", "label": "Claude Opus 4.5"},
        {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
    ]


class _FakeGeminiHttpxClient:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self._status_code = status_code
        self.requested_url = None
        self.requested_headers = None

    def get(self, url, *, headers=None):
        self.requested_url = url
        self.requested_headers = headers
        return httpx.Response(self._status_code, json=self._payload, request=httpx.Request("GET", url))


def test_list_models_filters_and_normalizes_gemini_catalog(monkeypatch) -> None:
    _settings(monkeypatch)
    fake_client = _FakeGeminiHttpxClient(
        {
            "models": [
                {
                    "name": "models/gemini-2.5-pro",
                    "displayName": "Gemini 2.5 Pro",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/text-embedding-004",
                    "displayName": "Text Embedding",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }
    )
    monkeypatch.setattr("app.llm.client.httpx.Client", lambda **_kwargs: fake_client)

    result = LLMClient(_config(api_protocol="gemini_generate_content")).list_models()

    assert result == [{"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"}]
    assert "api-key" in fake_client.requested_url or fake_client.requested_headers.get("x-goog-api-key") == "api-key"


class _FakePaginatedGeminiHttpxClient:
    """Serves two pages, keyed by the pageToken query param — a real catalog
    larger than one page's pageSize returns a nextPageToken the caller must
    follow, or the second page's models are silently dropped."""

    def __init__(self) -> None:
        self.requested_urls: list[str] = []

    def get(self, url, *, headers=None):
        self.requested_urls.append(url)
        if "pageToken=page-2-token" in url:
            payload = {
                "models": [
                    {
                        "name": "models/gemini-3.0-flash",
                        "displayName": "Gemini 3.0 Flash",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ]
            }
        else:
            payload = {
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "displayName": "Gemini 2.5 Pro",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ],
                "nextPageToken": "page-2-token",
            }
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


def test_list_models_follows_gemini_pagination_across_multiple_pages(monkeypatch) -> None:
    _settings(monkeypatch)
    fake_client = _FakePaginatedGeminiHttpxClient()
    monkeypatch.setattr("app.llm.client.httpx.Client", lambda **_kwargs: fake_client)

    result = LLMClient(_config(api_protocol="gemini_generate_content")).list_models()

    assert result == [
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
        {"id": "gemini-3.0-flash", "label": "Gemini 3.0 Flash"},
    ]
    assert len(fake_client.requested_urls) == 2
    assert "pageToken" not in fake_client.requested_urls[0]
    assert "pageToken=page-2-token" in fake_client.requested_urls[1]


def _codex_config():
    return SimpleNamespace(
        api_protocol="codex_app_server",
        api_key_encrypted="",
        base_url="",
        model="",
        temperature=0.2,
        max_output_tokens=1,
        extra_body_json={},
        protocol_options={},
    )


def _with_codex_subscription_service(monkeypatch, session) -> None:
    class _SubscriptionService:
        def create_session(self):
            return session

    import app.llm.client as client_module

    monkeypatch.setattr(client_module, "get_codex_subscription_service", lambda: _SubscriptionService())


def test_list_models_lists_the_real_codex_managed_catalog(monkeypatch) -> None:
    """本机 Codex app-server 有真实的 model/list JSON-RPC 方法，订阅渠道不是不可枚举。"""

    class _FakeSession:
        def __init__(self) -> None:
            self.closed = False

        def model_list(self):
            return {
                "data": [
                    {"id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "displayName": "GPT-5.6-Terra"},
                    {"id": "gpt-5.6-luna", "model": "gpt-5.6-luna", "displayName": "GPT-5.6-Luna"},
                ],
                "nextCursor": None,
            }

        def close(self):
            self.closed = True

    session = _FakeSession()
    _with_codex_subscription_service(monkeypatch, session)

    result = LLMClient(_codex_config()).list_models()

    assert result == [
        {"id": "gpt-5.6-terra", "label": "GPT-5.6-Terra"},
        {"id": "gpt-5.6-luna", "label": "GPT-5.6-Luna"},
    ]
    assert session.closed is True


def test_list_models_wraps_codex_runtime_errors_and_still_closes_the_session(monkeypatch) -> None:
    from app.codex_subscription import CodexSubscriptionError

    class _FailingSession:
        def __init__(self) -> None:
            self.closed = False

        def model_list(self):
            raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_TIMEOUT")

        def close(self):
            self.closed = True

    session = _FailingSession()
    _with_codex_subscription_service(monkeypatch, session)

    with pytest.raises(LLMError) as excinfo:
        LLMClient(_codex_config()).list_models()

    assert excinfo.value.code == "MODEL_SUBSCRIPTION_RUNTIME_TIMEOUT"
    assert session.closed is True


def test_list_models_wraps_provider_errors_as_llm_error(monkeypatch) -> None:
    _settings(monkeypatch)

    class _FailingModelsResource:
        def list(self):
            raise RuntimeError("upstream exploded")

    class _FailingOpenAIClient:
        def __init__(self, **_kwargs) -> None:
            self.models = _FailingModelsResource()

    monkeypatch.setattr("app.llm.client.OpenAI", _FailingOpenAIClient)

    with pytest.raises(LLMError):
        LLMClient(_config()).list_models()
