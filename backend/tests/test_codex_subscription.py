from __future__ import annotations

import importlib
from typing import Any

import pytest

from app.db.models import ModelConfig
from app.llm.client import LLMClient, LLMError


class ScriptedTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(params or {})
        self.calls.append((method, payload))
        if method == "account/read":
            return {
                "account": {
                    "type": "chatgpt",
                    "email": "secret@example.com",
                    "planType": "plus",
                    "accessToken": "never-return-this",
                }
            }
        if method == "account/login/start":
            return {
                "type": "chatgpt",
                "loginId": "login-private-id",
                "authUrl": "https://auth.example.test/?code=private-code",
            }
        if method == "account/login/cancel":
            return {}
        raise AssertionError(f"unexpected method: {method}")


def _subscription_module() -> Any:
    try:
        return importlib.import_module("app.codex_subscription.app_server")
    except ModuleNotFoundError:
        pytest.fail("subscription authentication must provide a local Codex app-server adapter")


def test_managed_subscription_login_returns_only_sanitized_account_state() -> None:
    module = _subscription_module()
    transport = ScriptedTransport()
    opened_urls: list[str] = []
    app_server = module.CodexAppServer(
        transport=transport,
        browser_opener=opened_urls.append,
    )

    connected = app_server.account_status()
    assert connected.status == "connected"
    assert connected.plan_type == "plus"
    assert connected.to_dict() == {
        "status": "connected",
        "plan_type": "plus",
        "message": "已连接 ChatGPT 订阅",
    }

    pending = app_server.start_login()
    assert pending.status == "pending"
    assert pending.to_dict() == {
        "status": "pending",
        "plan_type": None,
        "message": "已在默认浏览器中打开 ChatGPT 授权页面",
    }
    assert opened_urls == ["https://auth.example.test/?code=private-code"]
    assert "private-code" not in str(pending.to_dict())
    assert "login-private-id" not in str(pending.to_dict())

    cancelled = app_server.cancel_login()
    assert cancelled.status == "requires_login"
    assert transport.calls[-1] == ("account/login/cancel", {"loginId": "login-private-id"})


def test_subscription_status_leaves_pending_after_browser_login_completes() -> None:
    module = _subscription_module()

    class CompletingTransport:
        login_started = False

        def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if method == "account/read":
                return {
                    "account": {"type": "chatgpt", "planType": "plus"}
                    if self.login_started
                    else None
                }
            if method == "account/login/start":
                self.login_started = True
                return {
                    "loginId": "private-login-id",
                    "authUrl": "https://auth.example.test/private",
                }
            raise AssertionError(f"unexpected method: {method}")

    app_server = module.CodexAppServer(transport=CompletingTransport(), browser_opener=lambda _: True)

    assert app_server.start_login().status == "pending"
    assert app_server.account_status().status == "connected"


def test_global_app_server_uses_subscription_settings_and_closes_cleanly(monkeypatch) -> None:
    module = _subscription_module()
    if not hasattr(module, "get_settings"):
        pytest.fail("subscription app server must obtain its command and timeout from settings")

    created: list[Any] = []

    class FakeAppServer:
        def __init__(self, *, command, timeout_seconds) -> None:
            self.command = command
            self.timeout_seconds = timeout_seconds
            self.closed = False
            created.append(self)

        def close(self) -> None:
            self.closed = True

    settings = type(
        "Settings",
        (),
        {"codex_subscription_command": "codex-custom", "codex_subscription_timeout_seconds": 42.0},
    )()
    monkeypatch.setattr(module, "CodexAppServer", FakeAppServer)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    module.stop_codex_app_server()

    app_server = module.get_codex_app_server()

    assert app_server.command == ["codex-custom"]
    assert app_server.timeout_seconds == 42.0
    module.stop_codex_app_server()
    assert created == [app_server]
    assert app_server.closed is True


def test_subscription_driver_uses_a_constrained_ephemeral_codex_turn() -> None:
    module = _subscription_module()
    protocol_module = importlib.import_module("app.llm.protocol_drivers")
    if not hasattr(protocol_module, "CodexAppServerDriver"):
        pytest.fail("subscription models must use a dedicated Codex app-server protocol driver")

    class StreamingTransport(ScriptedTransport):
        def __init__(self) -> None:
            super().__init__()
            self.turn_requests: list[Any] = []

        def stream_turn(self, request: Any):
            self.turn_requests.append(request)
            yield "第一段"
            yield "第二段"

    transport = StreamingTransport()
    app_server = module.CodexAppServer(transport=transport)
    driver = protocol_module.CodexAppServerDriver(app_server, "gpt-5.1-codex")
    request = {
        "messages": [
            {"role": "system", "content": "你是 StaffDeck 助手。"},
            {"role": "user", "content": "请总结本周工作。"},
        ]
    }

    completion = driver.complete(request)
    chunks = list(driver.stream(request))

    assert completion.choices[0].message.content == "第一段第二段"
    assert "".join(chunk.choices[0].delta.content for chunk in chunks) == "第一段第二段"
    turn = transport.turn_requests[0]
    assert turn.ephemeral is True
    assert turn.sandbox == "read-only"
    assert turn.approval_policy == "never"
    assert turn.model == "gpt-5.1-codex"
    assert turn.system_prompt == "你是 StaffDeck 助手。"
    assert turn.user_prompt == "请总结本周工作。"


def test_llm_client_routes_subscription_models_without_decrypting_an_api_key(monkeypatch) -> None:
    client_module = importlib.import_module("app.llm.client")
    turns: list[dict[str, Any]] = []

    class AppServer:
        def stream_text(self, **kwargs: Any):
            turns.append(kwargs)
            yield "订阅响应"

    monkeypatch.setattr(client_module, "get_codex_app_server", lambda: AppServer())
    monkeypatch.setattr(
        client_module,
        "decrypt_secret",
        lambda _: pytest.fail("subscription models must not decrypt an API key"),
    )
    client = LLMClient(
        ModelConfig(
            tenant_id="tenant-1",
            name="Codex subscription",
            auth_mode="chatgpt_subscription",
            api_protocol="codex_app_server",
            api_key_encrypted="not-a-secret",
            model="gpt-5.1-codex",
        )
    )

    assert client.generate_text("系统提示", "用户输入") == "订阅响应"
    assert turns == [
        {
            "system_prompt": "系统提示",
            "user_prompt": "用户输入",
            "model": "gpt-5.1-codex",
            "cancellation": None,
        }
    ]


def test_subscription_model_supports_text_stream_and_json_calls(monkeypatch) -> None:
    client_module = importlib.import_module("app.llm.client")
    calls: list[dict[str, Any]] = []

    class AppServer:
        def stream_text(self, **kwargs: Any):
            calls.append(kwargs)
            prompt = kwargs["user_prompt"]
            if "Return exactly one valid json object" in prompt:
                yield '{"ready": true}'
            elif prompt == "请流式返回":
                yield "流式"
                yield "响应"
            else:
                yield "文本响应"

    monkeypatch.setattr(client_module, "get_codex_app_server", lambda: AppServer())
    monkeypatch.setattr(client_module, "decrypt_secret", lambda _: "unexpected")
    client = LLMClient(
        ModelConfig(
            tenant_id="tenant-1",
            name="Codex subscription",
            auth_mode="chatgpt_subscription",
            api_protocol="codex_app_server",
            model="gpt-5.1-codex",
        )
    )

    assert client.generate_text("系统", "请返回文本") == "文本响应"
    assert list(client.generate_text_stream("系统", "请流式返回")) == ["流式", "响应"]
    assert client.generate_json("系统", {"task": "返回 JSON"}) == {"ready": True}
    assert [call["model"] for call in calls] == ["gpt-5.1-codex"] * 3


def test_subscription_model_never_exposes_or_decrypts_oauth_or_api_key_material(monkeypatch) -> None:
    model_configs_module = importlib.import_module("app.api.model_configs")
    client_module = importlib.import_module("app.llm.client")
    row = ModelConfig(
        tenant_id="tenant-1",
        name="Codex subscription",
        auth_mode="chatgpt_subscription",
        api_protocol="codex_app_server",
        api_key_encrypted="api-key-must-not-be-read",
        model="gpt-5.1-codex",
    )
    monkeypatch.setattr(
        model_configs_module,
        "decrypt_secret",
        lambda _: pytest.fail("subscription model reads must not decrypt an API key"),
    )
    read_payload = model_configs_module.model_config_read(row).model_dump()

    class FailingAppServer:
        def stream_text(self, **_: Any):
            subscription_module = _subscription_module()
            raise subscription_module.CodexAppServerError(
                "MODEL_SUBSCRIPTION_AUTH_REQUIRED",
                "access_token=private-token",
            )
            yield "unreachable"

    monkeypatch.setattr(client_module, "get_codex_app_server", lambda: FailingAppServer())
    client = LLMClient(row)
    with pytest.raises(LLMError) as exc_info:
        client.generate_text("系统", "输入")

    serialized = f"{read_payload}{exc_info.value.public_detail()}"
    assert read_payload["api_key_masked"] == ""
    assert "api-key-must-not-be-read" not in serialized
    assert "private-token" not in serialized
    assert "access_token" not in serialized
