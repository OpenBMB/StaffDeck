from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.adapters.base import channel_reaction_token
from app.channels.adapters.dingtalk import (
    DINGTALK_ACK_EMOTION_BACKGROUND_ID,
    DINGTALK_ACK_EMOTION_ID,
    DINGTALK_ACK_EMOTION_NAME,
    DINGTALK_REACTION_HANDLE,
    DingTalkAdapter,
    DingTalkPermanentError,
    DingTalkTokenProvider,
    DingTalkTransientError,
    normalize_dingtalk_message,
    validate_dingtalk_webhook,
)
from app.channels.crypto import encrypt_channel_secret
from app.channels.service_dingtalk_inbox import (
    dingtalk_account_key,
    dingtalk_identity_scope,
    stage_dingtalk_inbound,
)
from app.channels.service_durable_inbox import StageDisposition
from app.channels.service_intake import _stage_received_reaction
from app.config import get_settings
from app.db.models import ChannelBinding, ChannelDelivery, ChannelInboundEvent, Tenant


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _raw(**overrides):
    value = {
        "msgtype": "text",
        "msgId": "msg-1",
        "conversationId": "conv-1",
        "conversationType": "1",
        "isInAtList": True,
        "senderStaffId": "staff-1",
        "senderNick": "Alice",
        "chatbotUserId": "robot-1",
        "chatbotCorpId": "corp-1",
        "sessionWebhook": "https://example.test/reply",
        "sessionWebhookExpiredTime": 9999999999999,
        "text": {"content": "hello"},
    }
    value.update(overrides)
    return value


def test_normalize_dingtalk_text_and_filters():
    inbound = normalize_dingtalk_message(_raw())
    assert inbound is not None
    assert inbound.event_id == "msg-1"
    assert inbound.from_user_id == "staff-1"
    assert inbound.text == "hello"
    assert normalize_dingtalk_message(_raw(msgtype="picture")) is None
    assert normalize_dingtalk_message(_raw(senderStaffId="robot-1")) is None
    assert normalize_dingtalk_message(_raw(conversationType="2", isInAtList=False)) is None
    grouped = normalize_dingtalk_message(
        _raw(conversationType="2", text={"content": " @robot-1 hello "})
    )
    assert grouped is not None
    assert grouped.text == "hello"
    assert validate_dingtalk_webhook("https://oapi.dingtalk.com/robot/send?session=x")
    assert not validate_dingtalk_webhook("https://attacker.example/steal")


def test_stage_dingtalk_is_deduplicated_and_fixes_tenant_scope():
    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        binding = ChannelBinding(
            tenant_id="tenant-1",
            agent_id="agent-1",
            channel="dingtalk",
            status="active",
            credentials_enc=encrypt_channel_secret("secret"),
            config_json={"client_id": "client-1"},
            external_account_key=dingtalk_account_key("client-1"),
            config_revision=3,
        )
        db.add(binding)
        db.commit()
        binding_id = binding.id
    inbound = normalize_dingtalk_message(_raw(), account_scope="corp-1")
    assert inbound is not None
    first = stage_dingtalk_inbound(
        db_engine=db_engine,
        binding_id=binding_id,
        expected_revision=3,
        client_id="client-1",
        tenant_key="corp-1",
        inbound=inbound,
    )
    second = stage_dingtalk_inbound(
        db_engine=db_engine,
        binding_id=binding_id,
        expected_revision=3,
        client_id="client-1",
        tenant_key="corp-1",
        inbound=inbound,
    )
    assert first.disposition is StageDisposition.STAGED
    assert second.disposition is StageDisposition.DUPLICATE
    with Session(db_engine) as db:
        events = db.exec(select(ChannelInboundEvent)).all()
        saved = db.get(ChannelBinding, binding_id)
        assert len(events) == 1
        assert events[0].target_json["context_token"] == "https://example.test/reply"
        assert events[0].target_json["to_user_id"] == "staff-1"
        assert saved.provider_tenant_key == "corp-1"
        assert saved.identity_scope_key == dingtalk_identity_scope("client-1", "corp-1")


def test_send_rejects_untrusted_webhook_and_uses_injected_client():
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"errcode": 0}

    class Client:
        def __init__(self):
            self.urls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, **_kwargs):
            self.urls.append(url)
            return Response()

    client = Client()
    adapter = DingTalkAdapter(client_factory=lambda: client)
    adapter.send(
        ChannelBinding(channel="dingtalk", tenant_id="t", agent_id="a"),
        {"session_webhook": "https://oapi.dingtalk.com/robot/send?session=x"},
        "hello",
        idempotency_key="delivery-1",
    )
    assert client.urls == ["https://oapi.dingtalk.com/robot/send?session=x"]


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload

    def json(self):
        return self._payload


class _RoutingClient:
    """按 URL 片段路由的假 httpx client；每个队列的最后一项会被重复返回。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, json=None, headers=None, **_kwargs):
        self.calls.append({"url": url, "body": json, "headers": headers or {}})
        for fragment, queue in self.routes.items():
            if fragment in url:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"未预期的请求地址 {url}")

    def calls_to(self, fragment):
        return [call for call in self.calls if fragment in call["url"]]


def _reaction_binding(**overrides):
    values = {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "channel": "dingtalk",
        "status": "active",
        "credentials_enc": encrypt_channel_secret("secret"),
        "config_json": {"client_id": "client-1"},
        "external_account_key": dingtalk_account_key("client-1"),
        "provider_tenant_key": "corp-1",
        "config_revision": 1,
    }
    values.update(overrides)
    return ChannelBinding(**values)


def _reaction_adapter(routes):
    client = _RoutingClient(routes)
    return DingTalkAdapter(client_factory=lambda: client), client


def _token_route(*tokens):
    return [_Response(200, {"accessToken": token, "expireIn": 7200}) for token in tokens]


def test_dingtalk_add_reaction_posts_thinking_emotion():
    adapter, client = _reaction_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "robot/emotion": [_Response(200)]}
    )
    handle = adapter.add_reaction(
        _reaction_binding(),
        {"message_id": "msg-1", "conversation_id": "conv-1"},
    )
    assert handle == DINGTALK_REACTION_HANDLE
    emotion_call = client.calls_to("robot/emotion")[0]
    assert emotion_call["url"].endswith("/robot/emotion/reply")
    assert emotion_call["headers"]["x-acs-dingtalk-access-token"] == "token-1"
    assert emotion_call["body"] == {
        "robotCode": "client-1",
        "openMsgId": "msg-1",
        "openConversationId": "conv-1",
        "emotionType": 2,
        "emotionName": DINGTALK_ACK_EMOTION_NAME,
        "textEmotion": {
            "emotionId": DINGTALK_ACK_EMOTION_ID,
            "emotionName": DINGTALK_ACK_EMOTION_NAME,
            "text": DINGTALK_ACK_EMOTION_NAME,
            "backgroundId": DINGTALK_ACK_EMOTION_BACKGROUND_ID,
        },
    }


def test_dingtalk_remove_reaction_recalls_with_symmetric_body():
    adapter, client = _reaction_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "robot/emotion": [_Response(200)]}
    )
    binding = _reaction_binding()
    target = {"message_id": "msg-1", "conversation_id": "conv-1"}
    adapter.remove_reaction(binding, target, DINGTALK_REACTION_HANDLE)
    recall_call = client.calls_to("robot/emotion")[0]
    assert recall_call["url"].endswith("/robot/emotion/recall")
    # recall 不依赖远端表情 ID，body 必须与 reply 完全一致，重复调用才安全。
    adapter_reply, reply_client = _reaction_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "robot/emotion": [_Response(200)]}
    )
    adapter_reply.add_reaction(binding, target)
    assert recall_call["body"] == reply_client.calls_to("robot/emotion")[0]["body"]


def test_dingtalk_emotion_rejects_incomplete_target():
    adapter, client = _reaction_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "robot/emotion": [_Response(200)]}
    )
    with pytest.raises(DingTalkPermanentError):
        adapter.add_reaction(_reaction_binding(), {"message_id": "msg-1"})
    with pytest.raises(DingTalkPermanentError):
        adapter.add_reaction(_reaction_binding(), {"conversation_id": "conv-1"})
    assert client.calls_to("robot/emotion") == []


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_Response(500), DingTalkTransientError),
        (_Response(429), DingTalkTransientError),
        (_Response(400, {"code": "system.err"}), DingTalkTransientError),
        (_Response(403, {"code": "Forbidden.AccessDenied"}), DingTalkPermanentError),
        (_Response(400, {"code": "invalidParameter.robotCode"}), DingTalkPermanentError),
    ],
)
def test_dingtalk_emotion_error_classification(response, expected):
    adapter, _client = _reaction_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "robot/emotion": [response]}
    )
    with pytest.raises(expected):
        adapter.add_reaction(
            _reaction_binding(),
            {"message_id": "msg-1", "conversation_id": "conv-1"},
        )


def test_dingtalk_emotion_refreshes_token_once_on_401():
    adapter, client = _reaction_adapter(
        {
            "oauth2/accessToken": _token_route("stale-token", "fresh-token"),
            "robot/emotion": [_Response(401), _Response(200)],
        }
    )
    adapter.add_reaction(
        _reaction_binding(),
        {"message_id": "msg-1", "conversation_id": "conv-1"},
    )
    emotion_tokens = [
        call["headers"]["x-acs-dingtalk-access-token"]
        for call in client.calls_to("robot/emotion")
    ]
    assert emotion_tokens == ["stale-token", "fresh-token"]
    assert len(client.calls_to("oauth2/accessToken")) == 2


def test_dingtalk_token_is_cached_until_config_revision_changes():
    client = _RoutingClient({"oauth2/accessToken": _token_route("token-1", "token-2")})
    provider = DingTalkTokenProvider(client_factory=lambda: client)
    binding = _reaction_binding()
    assert provider.get(binding) == "token-1"
    assert provider.get(binding) == "token-1"
    assert len(client.calls_to("oauth2/accessToken")) == 1
    binding.config_revision = 2
    assert provider.get(binding) == "token-2"
    assert len(client.calls_to("oauth2/accessToken")) == 2


def test_channel_reaction_token_gates_by_adapter_capability():
    # 钉钉与飞书都声明了 reaction 能力；微信没有，intake 据此跳过登记。
    assert channel_reaction_token("dingtalk") == DINGTALK_ACK_EMOTION_NAME
    assert channel_reaction_token("wechat") is None
    assert channel_reaction_token("unregistered-channel") is None


def _enable_dingtalk_reaction(monkeypatch) -> None:
    settings = get_settings().model_copy(
        update={"channel_dingtalk_reaction_enabled": True}
    )
    monkeypatch.setattr("app.channels.service_intake.get_settings", lambda: settings)


def _seed_reaction_binding_and_event(db) -> tuple[ChannelBinding, ChannelInboundEvent]:
    db.add(Tenant(id="tenant-1", name="Tenant"))
    binding = ChannelBinding(
        tenant_id="tenant-1",
        agent_id="agent-1",
        channel="dingtalk",
        status="active",
        credentials_enc=encrypt_channel_secret("secret"),
        config_json={"client_id": "client-1"},
        external_account_key=dingtalk_account_key("client-1"),
    )
    db.add(binding)
    db.commit()
    event = ChannelInboundEvent(
        tenant_id="tenant-1",
        binding_id=binding.id,
        channel="dingtalk",
        event_id="msg-1",
        target_json={"message_id": "msg-1", "conversation_id": "conv-1"},
        status="processing",
    )
    db.add(event)
    db.commit()
    return binding, event


def test_dingtalk_reaction_staging_is_off_until_verified():
    # 默认关闭：emotion 常量与权限真机验证通过前不得给每条消息登记投递。
    db_engine = _engine()
    with Session(db_engine) as db:
        binding, event = _seed_reaction_binding_and_event(db)
        _stage_received_reaction(db, binding, event)
        db.commit()
        assert db.exec(select(ChannelDelivery)).all() == []


def test_dingtalk_inbound_event_stages_reaction_add(monkeypatch):
    _enable_dingtalk_reaction(monkeypatch)
    db_engine = _engine()
    with Session(db_engine) as db:
        binding, event = _seed_reaction_binding_and_event(db)

        _stage_received_reaction(db, binding, event)
        db.commit()

        delivery = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "reaction_add")
        ).one()
        assert delivery.idempotency_key == f"dingtalk-reaction-add:{binding.id}:msg-1"
        assert delivery.text == DINGTALK_ACK_EMOTION_NAME
        # conversation_id 必须从入站事件继承，emotion 接口需要 openConversationId。
        assert delivery.target_json == {
            "message_id": "msg-1",
            "event_pk": event.id,
            "conversation_id": "conv-1",
        }

        # 同一事件重复登记不得产生第二条投递。
        _stage_received_reaction(db, binding, event)
        db.commit()
        assert len(db.exec(select(ChannelDelivery)).all()) == 1


def test_dingtalk_token_errors_are_classified():
    binding = _reaction_binding()
    transient = DingTalkTokenProvider(
        client_factory=lambda: _RoutingClient({"oauth2/accessToken": [_Response(500)]})
    )
    with pytest.raises(DingTalkTransientError):
        transient.get(binding)
    permanent = DingTalkTokenProvider(
        client_factory=lambda: _RoutingClient({"oauth2/accessToken": [_Response(401)]})
    )
    with pytest.raises(DingTalkPermanentError):
        permanent.get(binding)
    missing_field = DingTalkTokenProvider(
        client_factory=lambda: _RoutingClient(
            {"oauth2/accessToken": [_Response(200, {"expireIn": 7200})]}
        )
    )
    with pytest.raises(DingTalkTransientError):
        missing_field.get(binding)
