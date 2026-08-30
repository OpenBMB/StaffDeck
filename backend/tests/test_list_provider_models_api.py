from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.model_configs import list_provider_models
from app.db.models import User
from app.llm import LLMError
from app.llm.schemas import ModelListModelsRequest


def _admin(tenant_id: str = "tenant_a") -> User:
    return User(
        id="user_admin",
        tenant_id=tenant_id,
        username="admin",
        role="admin",
        password_hash="unused",
    )


def _member(tenant_id: str = "tenant_a") -> User:
    return User(
        id="user_member",
        tenant_id=tenant_id,
        username="member",
        role="member",
        password_hash="unused",
    )


def test_list_provider_models_rejects_non_admin_users(monkeypatch) -> None:
    request = ModelListModelsRequest(
        tenant_id="tenant_a",
        api_protocol="openai_chat_completions",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
    )

    with pytest.raises(HTTPException) as excinfo:
        list_provider_models(request, current_user=_member())

    assert excinfo.value.status_code == 403


def test_list_provider_models_returns_normalized_options_on_success(monkeypatch) -> None:
    captured = {}

    class _FakeLLMClient:
        def __init__(self, config) -> None:
            captured["config"] = config

        def list_models(self):
            return [{"id": "gpt-4o", "label": "gpt-4o"}]

    monkeypatch.setattr("app.api.model_configs.LLMClient", _FakeLLMClient)

    request = ModelListModelsRequest(
        tenant_id="tenant_a",
        api_protocol="openai_chat_completions",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
    )

    result = list_provider_models(request, current_user=_admin())

    assert result.success is True
    assert [option.model_dump() for option in result.models] == [{"id": "gpt-4o", "label": "gpt-4o"}]
    assert captured["config"].api_key_encrypted != "sk-test"  # must be encrypted, not stored raw


def test_list_provider_models_returns_failure_payload_instead_of_raising(monkeypatch) -> None:
    class _FailingLLMClient:
        def __init__(self, config) -> None:
            pass

        def list_models(self):
            raise LLMError("MODEL_AUTHENTICATION_FAILED", code="MODEL_AUTHENTICATION_FAILED", status_code=401)

    monkeypatch.setattr("app.api.model_configs.LLMClient", _FailingLLMClient)

    request = ModelListModelsRequest(
        tenant_id="tenant_a",
        api_protocol="openai_chat_completions",
        base_url="https://api.openai.com/v1",
        api_key="sk-bad-key",
    )

    result = list_provider_models(request, current_user=_admin())

    assert result.success is False
    assert result.models == []
    assert result.error is not None
    assert result.error.code == "MODEL_AUTHENTICATION_FAILED"


def test_list_provider_models_lists_codex_subscription_models_without_requiring_an_api_key(monkeypatch) -> None:
    """本机 Codex app-server 有真实的 model/list 方法，订阅渠道不需要 API Key 也能走 LLMClient。"""
    captured = {}

    class _FakeLLMClient:
        def __init__(self, config) -> None:
            captured["config"] = config

        def list_models(self):
            return [{"id": "gpt-5.6-terra", "label": "GPT-5.6-Terra"}]

    monkeypatch.setattr("app.api.model_configs.LLMClient", _FakeLLMClient)

    request = ModelListModelsRequest(
        tenant_id="tenant_a",
        api_protocol="codex_app_server",
        base_url=None,
        api_key=None,
    )

    result = list_provider_models(request, current_user=_admin())

    assert result.success is True
    assert [option.model_dump() for option in result.models] == [
        {"id": "gpt-5.6-terra", "label": "GPT-5.6-Terra"}
    ]
    assert captured["config"].api_protocol.value == "codex_app_server"


def test_list_provider_models_returns_failure_payload_for_a_failed_codex_call(monkeypatch) -> None:
    class _FailingLLMClient:
        def __init__(self, config) -> None:
            pass

        def list_models(self):
            raise LLMError(
                "MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE",
                code="MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE",
            )

    monkeypatch.setattr("app.api.model_configs.LLMClient", _FailingLLMClient)

    request = ModelListModelsRequest(
        tenant_id="tenant_a",
        api_protocol="codex_app_server",
        base_url=None,
        api_key=None,
    )

    result = list_provider_models(request, current_user=_admin())

    assert result.success is False
    assert result.models == []
    assert result.error is not None
    assert result.error.code == "MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE"
