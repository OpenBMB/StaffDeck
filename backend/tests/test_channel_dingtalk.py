from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.adapters.base import channel_reaction_token
from app.channels.adapters.dingtalk import (
    DINGTALK_ACK_EMOTION_BACKGROUND_ID,
    DINGTALK_ACK_EMOTION_ID,
    DINGTALK_ACK_EMOTION_NAME,
    DINGTALK_REACTION_HANDLE,
    DINGTALK_TEXT_LIMIT,
    DINGTALK_TRACE_CARD_TEMPLATE_ID,
    DingTalkAdapter,
    DingTalkPermanentError,
    DingTalkTokenProvider,
    DingTalkTransientError,
    normalize_dingtalk_message,
    validate_dingtalk_webhook,
)
from app.channels.crypto import encrypt_channel_secret
from app.channels.service_dingtalk_inbox import (
    decode_replay_envelope,
    dingtalk_account_key,
    dingtalk_identity_scope,
    encode_replay_envelope,
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
    def __init__(self, status_code=200, payload=None, content: bytes = b""):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.content = content

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

    def _route(self, url, body, headers, method):
        self.calls.append({"url": url, "body": body, "headers": headers or {}, "method": method})
        for fragment, queue in self.routes.items():
            if fragment in url:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"未预期的请求地址 {url}")

    def post(self, url, json=None, headers=None, **_kwargs):
        return self._route(url, json, headers, "POST")

    def put(self, url, json=None, headers=None, **_kwargs):
        return self._route(url, json, headers, "PUT")

    def get(self, url, headers=None, **_kwargs):
        return self._route(url, None, headers, "GET")

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


def test_normalize_dingtalk_picture_message_extracts_attachment() -> None:
    from app.channels.adapters.base import ChannelInboundAttachment

    raw = _raw(
        msgtype="picture",
        content={"downloadCode": "dc_001", "pictureDownloadCode": "dc_001"},
    )
    inbound = normalize_dingtalk_message(raw)
    assert inbound is not None
    # 图片消息无文本,但附件存在所以不再被丢弃
    assert inbound.text == ""
    assert len(inbound.attachments) == 1
    att = inbound.attachments[0]
    assert isinstance(att, ChannelInboundAttachment)
    assert att.media_id == "dc_001"
    assert att.kind == "image"
    assert att.filename == "dc_001"  # 不在 normalize 阶段硬编码扩展名
    assert att.content_type == ""  # 下载后由 _resolve_content_type 推断
    assert att.download_params == {"download_code": "dc_001", "type": "picture"}


def test_normalize_dingtalk_file_message_extracts_attachment_with_filename() -> None:
    raw = _raw(
        msgtype="file",
        content={"downloadCode": "dc_002", "fileName": "report.pdf"},
    )
    inbound = normalize_dingtalk_message(raw)
    assert inbound is not None
    assert inbound.text == ""
    assert len(inbound.attachments) == 1
    att = inbound.attachments[0]
    assert att.media_id == "dc_002"
    assert att.kind == "file"
    assert att.filename == "report.pdf"
    assert att.content_type == ""
    assert att.download_params == {"download_code": "dc_002", "type": "file"}


def test_normalize_dingtalk_text_message_has_empty_attachments() -> None:
    """纯文本消息仍正常工作,attachments 为空列表。"""
    inbound = normalize_dingtalk_message(_raw())
    assert inbound is not None
    assert inbound.text == "hello"
    assert inbound.attachments == []


def test_normalize_dingtalk_picture_without_download_code_returns_none() -> None:
    """picture 消息但缺 downloadCode 时返回 None(无文本也无附件)。"""
    raw = _raw(msgtype="picture", content={})
    assert normalize_dingtalk_message(raw) is None


def test_normalize_dingtalk_picture_in_group_requires_at() -> None:
    """群聊 picture 消息仍需 isInAtList=True 才通过。"""
    raw = _raw(
        msgtype="picture",
        conversationType="2",
        isInAtList=False,
        content={"downloadCode": "dc_group"},
    )
    assert normalize_dingtalk_message(raw) is None

    raw_ok = _raw(
        msgtype="picture",
        conversationType="2",
        isInAtList=True,
        content={"downloadCode": "dc_group_ok"},
    )
    inbound = normalize_dingtalk_message(raw_ok)
    assert inbound is not None
    assert inbound.is_group is True
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].media_id == "dc_group_ok"


def test_normalize_dingtalk_richtext_extracts_attachment_and_text() -> None:
    """richtext 消息提取图片附件和文本。"""
    from app.channels.adapters.base import ChannelInboundAttachment

    raw = _raw(
        msgtype="richText",
        conversationType="2",
        isInAtList=True,
        content={
            "richText": [
                {"type": "picture", "downloadCode": "dc_rt_1", "pictureDownloadCode": "dc_rt_1"},
                {"text": "\n"},
                {"text": "@staffdeck渠道接入测试机器人"},
            ]
        },
    )
    inbound = normalize_dingtalk_message(raw)
    assert inbound is not None
    assert inbound.is_group is True
    assert len(inbound.attachments) == 1
    att = inbound.attachments[0]
    assert isinstance(att, ChannelInboundAttachment)
    assert att.media_id == "dc_rt_1"
    assert att.kind == "image"
    assert att.download_params == {"download_code": "dc_rt_1", "type": "picture"}
    # richtext 文本被拼接,@mention 被 strip
    assert inbound.text == ""


def test_normalize_dingtalk_richtext_without_download_code_returns_none() -> None:
    """richtext 消息但无 picture downloadCode 且无文本时返回 None。"""
    raw = _raw(
        msgtype="richText",
        content={"richText": [{"type": "picture"}, {"text": "\n"}]},
    )
    assert normalize_dingtalk_message(raw) is None


def _download_media_adapter(*, token_responses=None, download_responses=None,
                            file_response=None):
    """构造用于 download_media 测试的 adapter + client。

    token_responses: oauth2/accessToken 队列
    download_responses: messageFiles/download 队列(返回 downloadUrl JSON)
    file_response: 第二步 GET downloadUrl 的 _Response
    """
    routes = {
        "oauth2/accessToken": token_responses or _token_route("token-1"),
        "robot/messageFiles/download": download_responses
        or [_Response(200, {"downloadUrl": "https://download.example/file.bin"})],
        "download.example": [file_response or _Response(200, content=b"FILE-BYTES")],
    }
    client = _RoutingClient(routes)
    return DingTalkAdapter(client_factory=lambda: client), client


def test_download_media_calls_two_step_download() -> None:
    from app.channels.adapters.base import ChannelInboundAttachment

    adapter, client = _download_media_adapter(
        file_response=_Response(200, content=b"FILE-BYTES"),
    )
    att = ChannelInboundAttachment(
        media_id="dc_001",
        kind="file",
        filename="report.pdf",
        download_params={"download_code": "dc_001", "type": "file"},
    )
    data = adapter.download_media(_reaction_binding(), att)
    assert data == b"FILE-BYTES"

    # 第一步: POST messageFiles/download
    download_call = client.calls_to("robot/messageFiles/download")[0]
    assert download_call["method"] == "POST"
    assert download_call["body"] == {"downloadCode": "dc_001", "robotCode": "client-1"}
    assert download_call["headers"]["x-acs-dingtalk-access-token"] == "token-1"
    assert download_call["headers"]["Content-Type"] == "application/json"

    # 第二步: GET downloadUrl
    file_call = client.calls_to("download.example")[0]
    assert file_call["method"] == "GET"
    assert file_call["url"] == "https://download.example/file.bin"


def test_download_media_missing_download_code_is_permanent_error() -> None:
    from app.channels.adapters.base import ChannelInboundAttachment

    adapter, _client = _download_media_adapter()
    att = ChannelInboundAttachment(
        media_id="",
        kind="file",
        download_params={"download_code": "", "type": "file"},
    )
    with pytest.raises(DingTalkPermanentError, match="缺少 downloadCode"):
        adapter.download_media(_reaction_binding(), att)


def test_download_media_missing_download_url_is_permanent_error() -> None:
    from app.channels.adapters.base import ChannelInboundAttachment

    # 响应 200 但缺 downloadUrl
    adapter, _client = _download_media_adapter(
        download_responses=[_Response(200, {})],
    )
    att = ChannelInboundAttachment(
        media_id="dc_x",
        kind="file",
        download_params={"download_code": "dc_x", "type": "file"},
    )
    with pytest.raises(DingTalkPermanentError, match="缺少 downloadUrl"):
        adapter.download_media(_reaction_binding(), att)


def test_download_media_refreshes_token_on_401() -> None:
    from app.channels.adapters.base import ChannelInboundAttachment

    adapter, client = _download_media_adapter(
        token_responses=_token_route("stale-token", "fresh-token"),
        download_responses=[
            _Response(401),  # 第一次 401
            _Response(200, {"downloadUrl": "https://download.example/file.bin"}),
        ],
        file_response=_Response(200, content=b"OK"),
    )
    att = ChannelInboundAttachment(
        media_id="dc_001",
        kind="file",
        download_params={"download_code": "dc_001", "type": "file"},
    )
    data = adapter.download_media(_reaction_binding(), att)
    assert data == b"OK"

    download_calls = client.calls_to("robot/messageFiles/download")
    assert len(download_calls) == 2
    tokens_used = [c["headers"]["x-acs-dingtalk-access-token"] for c in download_calls]
    assert tokens_used == ["stale-token", "fresh-token"]
    # token 刷新两次
    assert len(client.calls_to("oauth2/accessToken")) == 2


def test_download_media_5xx_on_first_step_is_transient() -> None:
    from app.channels.adapters.base import ChannelInboundAttachment

    adapter, _client = _download_media_adapter(
        download_responses=[_Response(503)],
    )
    att = ChannelInboundAttachment(
        media_id="dc_001",
        kind="file",
        download_params={"download_code": "dc_001", "type": "file"},
    )
    with pytest.raises(DingTalkTransientError, match="暂时不可用"):
        adapter.download_media(_reaction_binding(), att)


def test_download_media_non_200_on_second_step_is_transient() -> None:
    from app.channels.adapters.base import ChannelInboundAttachment

    adapter, _client = _download_media_adapter(
        file_response=_Response(404, content=b""),
    )
    att = ChannelInboundAttachment(
        media_id="dc_001",
        kind="file",
        download_params={"download_code": "dc_001", "type": "file"},
    )
    with pytest.raises(DingTalkTransientError, match="HTTP 404"):
        adapter.download_media(_reaction_binding(), att)


def test_envelope_round_trips_attachments_as_dataclass() -> None:
    from app.channels.adapters.base import ChannelInbound, ChannelInboundAttachment

    inbound = ChannelInbound(
        channel="dingtalk",
        event_id="msg_att_1",
        from_user_id="staff-1",
        to_user_id="robot-1",
        session_id="conv-1",
        group_id="",
        context_token="https://example.test/reply",
        text="",
        is_group=False,
        raw={"msgId": "msg_att_1"},
        sender_name="Alice",
        account_scope="corp-1",
        attachments=[
            ChannelInboundAttachment(
                media_id="dc_001",
                kind="picture",
                download_params={"download_code": "dc_001", "type": "picture"},
            ),
            ChannelInboundAttachment(
                media_id="dc_002",
                kind="file",
                filename="report.xlsx",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                download_params={"download_code": "dc_002", "type": "file"},
            ),
        ],
    )
    envelope = encode_replay_envelope(inbound, client_id="client-1", tenant_key="corp-1")
    decoded = decode_replay_envelope(envelope)
    assert len(decoded.attachments) == 2
    for att in decoded.attachments:
        assert isinstance(att, ChannelInboundAttachment)
    assert decoded.attachments[0].media_id == "dc_001"
    assert decoded.attachments[0].download_params["download_code"] == "dc_001"
    assert decoded.attachments[1].filename == "report.xlsx"
    assert decoded.attachments[1].kind == "file"


# ---------------------------------------------------------------------------
# 富文本（markdown）渲染 — channel-render-plan §5.3
# ---------------------------------------------------------------------------


class _WebhookClient:
    """记录所有 webhook POST 请求的假 client。"""

    def __init__(self, response=None):
        self.calls = []
        self._response = response or _Response(200, {"errcode": 0})

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, json=None, headers=None, **_kwargs):
        self.calls.append({"url": url, "body": json, "headers": headers or {}})
        return self._response


def _send_binding():
    return ChannelBinding(
        tenant_id="t",
        agent_id="a",
        channel="dingtalk",
        config_json={"client_id": "client-1"},
        credentials_enc=encrypt_channel_secret("secret"),
    )


def _send_target():
    return {"session_webhook": "https://oapi.dingtalk.com/robot/send?session=x"}


def test_dingtalk_markdown_render_uses_markdown_msgtype():
    client = _WebhookClient()
    adapter = DingTalkAdapter(client_factory=lambda: client)
    adapter.send(_send_binding(), _send_target(), "**粗体** 列表", idempotency_key="d1")
    assert len(client.calls) == 1
    body = client.calls[0]["body"]
    assert body["msgtype"] == "markdown"
    assert body["markdown"]["text"] == "**粗体** 列表"
    assert body["markdown"]["title"]


def test_dingtalk_markdown_title_from_heading():
    client = _WebhookClient()
    adapter = DingTalkAdapter(client_factory=lambda: client)
    adapter.send(_send_binding(), _send_target(), "# 周报\n正文", idempotency_key="d2")
    body = client.calls[0]["body"]
    assert body["markdown"]["title"] == "周报"


def test_dingtalk_markdown_title_from_first_line_when_no_heading():
    client = _WebhookClient()
    adapter = DingTalkAdapter(client_factory=lambda: client)
    # 含 markdown 语法（粗体）但无标题，title 取首行截断
    adapter.send(_send_binding(), _send_target(), "**首行**\n第二行", idempotency_key="d3")
    body = client.calls[0]["body"]
    assert body["msgtype"] == "markdown"
    assert body["markdown"]["title"] == "**首行**"


def test_dingtalk_plain_text_still_uses_text_msgtype():
    client = _WebhookClient()
    adapter = DingTalkAdapter(client_factory=lambda: client)
    adapter.send(_send_binding(), _send_target(), "hello world", idempotency_key="d4")
    body = client.calls[0]["body"]
    assert body["msgtype"] == "text"
    assert body["text"]["content"] == "hello world"


def test_dingtalk_rich_render_disabled_falls_back_to_text(monkeypatch):
    settings = get_settings().model_copy(update={"channel_rich_render_enabled": False})
    monkeypatch.setattr("app.channels.adapters.dingtalk.get_settings", lambda: settings)
    client = _WebhookClient()
    adapter = DingTalkAdapter(client_factory=lambda: client)
    adapter.send(_send_binding(), _send_target(), "**粗体**", idempotency_key="d5")
    body = client.calls[0]["body"]
    assert body["msgtype"] == "text"
    assert body["text"]["content"] == "**粗体**"


def test_dingtalk_markdown_long_text_chunked_with_titles():
    client = _WebhookClient()
    adapter = DingTalkAdapter(client_factory=lambda: client)
    long_md = "\n".join(f"# 标题{i}\n内容{i}" for i in range(800))
    adapter.send(_send_binding(), _send_target(), long_md, idempotency_key="d6")
    assert len(client.calls) >= 2
    for call in client.calls:
        assert call["body"]["msgtype"] == "markdown"
        assert call["body"]["markdown"]["title"]
        assert len(call["body"]["markdown"]["text"]) <= DINGTALK_TEXT_LIMIT


def test_dingtalk_markdown_rejects_untrusted_webhook():
    client = _WebhookClient()
    adapter = DingTalkAdapter(client_factory=lambda: client)
    with pytest.raises(DingTalkPermanentError):
        adapter.send(
            _send_binding(),
            {"session_webhook": "https://attacker.example/steal"},
            "**x**",
            idempotency_key="d7",
        )
    assert client.calls == []


def test_dingtalk_markdown_rejects_expired_webhook():
    client = _WebhookClient()
    adapter = DingTalkAdapter(client_factory=lambda: client)
    target = {
        "session_webhook": "https://oapi.dingtalk.com/robot/send?session=x",
        "session_webhook_expired_time": 1,
    }
    with pytest.raises(DingTalkPermanentError, match="过期"):
        adapter.send(_send_binding(), target, "**x**", idempotency_key="d8")
    assert client.calls == []


def test_dingtalk_markdown_send_failure_is_permanent():
    client = _WebhookClient(_Response(400, {"errcode": 1}))
    adapter = DingTalkAdapter(client_factory=lambda: client)
    with pytest.raises(DingTalkPermanentError):
        adapter.send(_send_binding(), _send_target(), "**x**", idempotency_key="d9")


def test_dingtalk_markdown_5xx_is_transient():
    client = _WebhookClient(_Response(503, {"errcode": 0}))
    adapter = DingTalkAdapter(client_factory=lambda: client)
    with pytest.raises(DingTalkTransientError):
        adapter.send(_send_binding(), _send_target(), "**x**", idempotency_key="d10")


def test_dingtalk_overlong_fenced_code_block_chunks_are_balanced():
    """回归 P1：超长围栏代码块切分后，每个发送 chunk 必须围栏平衡（合法 Markdown）。"""
    client = _WebhookClient()
    adapter = DingTalkAdapter(client_factory=lambda: client)
    code_lines = [f"line_{i} = {i}" for i in range(300)]
    text = "```python\n" + "\n".join(code_lines) + "\n```"
    adapter.send(_send_binding(), _send_target(), text, idempotency_key="d-long-code")
    assert len(client.calls) >= 2
    for call in client.calls:
        body = call["body"]
        assert body["msgtype"] == "markdown"
        md_text = body["markdown"]["text"]
        fence_count = sum(
            1 for line in md_text.split("\n") if line.strip().startswith("```")
        )
        assert fence_count % 2 == 0, "fenced code block split across messages is unbalanced"
        assert len(md_text) <= DINGTALK_TEXT_LIMIT


# ---- 卡片实例（trace 流式卡片）----


def _card_binding(**overrides):
    return _reaction_binding(**overrides)


def _card_adapter(routes):
    client = _RoutingClient(routes)
    return DingTalkAdapter(client_factory=lambda: client), client


_CARD_DATA = {
    "msgTitle": "正在思考…",
    "msgContent": "等待执行步骤…",
    "flowStatus": "1",
    "sys_full_json_obj": '{"order": ["msgTitle", "msgContent"]}',
}


def test_dingtalk_create_card_delivers_to_robot_space_for_single_chat():
    adapter, client = _card_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "card/instances": [_Response(200)]}
    )
    out_track_id = adapter.create_card(
        _card_binding(),
        {
            "to_user_id": "staff-1",
            "conversation_id": "cid-1",
            "conversation_type": "1",
            "session_webhook": "https://oapi.dingtalk.com/robot/sendBySession?session=x",
        },
        _CARD_DATA,
        idempotency_key="dingtalk-trace:chan-1:turn-1",
    )
    assert out_track_id == hashlib.sha256(b"dingtalk-trace:chan-1:turn-1").hexdigest()
    create_call = client.calls_to("card/instances/createAndDeliver")[0]
    assert create_call["method"] == "POST"
    assert create_call["headers"]["x-acs-dingtalk-access-token"] == "token-1"
    assert create_call["body"] == {
        "cardTemplateId": DINGTALK_TRACE_CARD_TEMPLATE_ID,
        "outTrackId": out_track_id,
        "cardData": {"cardParamMap": _CARD_DATA},
        "callbackType": "STREAM",
        "imGroupOpenSpaceModel": {"supportForward": False},
        "imRobotOpenSpaceModel": {"supportForward": False},
        "openSpaceId": "dtv1.card//IM_ROBOT.staff-1",
        "imRobotOpenDeliverModel": {"spaceType": "IM_ROBOT"},
    }


def test_dingtalk_create_card_delivers_to_group_space():
    adapter, client = _card_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "card/instances": [_Response(200)]}
    )
    adapter.create_card(
        _card_binding(),
        {
            "to_user_id": "cid-group",
            "conversation_id": "cid-group",
            "conversation_type": "2",
            "session_webhook": "https://oapi.dingtalk.com/robot/sendBySession?session=x",
        },
        _CARD_DATA,
        idempotency_key="dingtalk-trace:chan-1:turn-2",
    )
    create_call = client.calls_to("card/instances/createAndDeliver")[0]
    body = create_call["body"]
    assert body["openSpaceId"] == "dtv1.card//IM_GROUP.cid-group"
    assert body["imGroupOpenDeliverModel"] == {"robotCode": "client-1"}
    assert "imRobotOpenDeliverModel" not in body


def test_dingtalk_create_card_uses_custom_template_from_config():
    adapter, client = _card_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "card/instances": [_Response(200)]}
    )
    adapter.create_card(
        _card_binding(config_json={"client_id": "client-1", "card_template_id": "tpl-9.schema"}),
        {"to_user_id": "staff-1", "conversation_type": "1"},
        _CARD_DATA,
        idempotency_key="dingtalk-trace:chan-1:turn-3",
    )
    create_call = client.calls_to("card/instances/createAndDeliver")[0]
    assert create_call["body"]["cardTemplateId"] == "tpl-9.schema"


def test_dingtalk_create_card_rejects_invalid_input():
    adapter, _client = _card_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "card/instances": [_Response(200)]}
    )
    with pytest.raises(DingTalkPermanentError, match="幂等键"):
        adapter.create_card(_card_binding(), {"to_user_id": "staff-1"}, _CARD_DATA, idempotency_key="")
    with pytest.raises(DingTalkPermanentError, match="卡片数据"):
        adapter.create_card(_card_binding(), {"to_user_id": "staff-1"}, {}, idempotency_key="k")
    # 单聊缺少用户标识、群聊缺少会话标识均为永久错误
    with pytest.raises(DingTalkPermanentError, match="用户标识"):
        adapter.create_card(_card_binding(), {"conversation_type": "1"}, _CARD_DATA, idempotency_key="k")
    with pytest.raises(DingTalkPermanentError, match="会话标识"):
        adapter.create_card(_card_binding(), {"conversation_type": "2"}, _CARD_DATA, idempotency_key="k")


def test_dingtalk_create_card_refreshes_token_once_on_401():
    adapter, client = _card_adapter(
        {
            "oauth2/accessToken": _token_route("stale-token", "fresh-token"),
            "card/instances": [_Response(401), _Response(200)],
        }
    )
    adapter.create_card(
        _card_binding(),
        {"to_user_id": "staff-1", "conversation_type": "1"},
        _CARD_DATA,
        idempotency_key="dingtalk-trace:chan-1:turn-4",
    )
    tokens = [
        call["headers"]["x-acs-dingtalk-access-token"]
        for call in client.calls_to("card/instances/createAndDeliver")
    ]
    assert tokens == ["stale-token", "fresh-token"]
    assert len(client.calls_to("oauth2/accessToken")) == 2


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_Response(500), DingTalkTransientError),
        (_Response(429), DingTalkTransientError),
        (_Response(403, {"code": "Forbidden.AccessDenied", "message": "access denied"}), DingTalkPermanentError),
        (_Response(400, {"code": "invalidParameter.outTrackId"}), DingTalkPermanentError),
        (_Response(400, {"code": "system.err"}), DingTalkTransientError),
    ],
)
def test_dingtalk_create_card_error_classification(response, expected):
    adapter, _client = _card_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "card/instances": [response]}
    )
    with pytest.raises(expected):
        adapter.create_card(
            _card_binding(),
            {"to_user_id": "staff-1", "conversation_type": "1"},
            _CARD_DATA,
            idempotency_key="dingtalk-trace:chan-1:turn-5",
        )


def test_dingtalk_create_card_403_includes_permission_guidance():
    """403 时错误信息应携带钉钉原始 code 与开通权限的指引，便于排查。"""
    adapter, _client = _card_adapter(
        {
            "oauth2/accessToken": _token_route("token-1"),
            "card/instances": [
                _Response(403, {"code": "Forbidden.AccessDenied", "message": "no permission"})
            ],
        }
    )
    with pytest.raises(DingTalkPermanentError) as exc_info:
        adapter.create_card(
            _card_binding(),
            {"to_user_id": "staff-1", "conversation_type": "1"},
            _CARD_DATA,
            idempotency_key="dingtalk-trace:chan-1:turn-6",
        )
    message = str(exc_info.value)
    assert "Forbidden.AccessDenied" in message
    assert "no permission" in message
    assert "Card.Instance.Write" in message


def test_dingtalk_update_card_puts_full_card_data():
    adapter, client = _card_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "card/instances": [_Response(200)]}
    )
    adapter.update_card(_card_binding(), "ot-123", _CARD_DATA)
    update_calls = [
        call for call in client.calls_to("card/instances") if call["method"] == "PUT"
    ]
    assert len(update_calls) == 1
    call = update_calls[0]
    assert call["url"].endswith("/card/instances")
    assert call["headers"]["x-acs-dingtalk-access-token"] == "token-1"
    assert call["body"] == {
        "outTrackId": "ot-123",
        "cardData": {"cardParamMap": _CARD_DATA},
    }


def test_dingtalk_update_card_rejects_invalid_input():
    adapter, _client = _card_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "card/instances": [_Response(200)]}
    )
    with pytest.raises(DingTalkPermanentError, match="实例标识"):
        adapter.update_card(_card_binding(), "", _CARD_DATA)
    with pytest.raises(DingTalkPermanentError, match="卡片数据"):
        adapter.update_card(_card_binding(), "ot-123", {})


def test_dingtalk_update_card_refreshes_token_once_on_401():
    adapter, client = _card_adapter(
        {
            "oauth2/accessToken": _token_route("stale-token", "fresh-token"),
            "card/instances": [_Response(401), _Response(200)],
        }
    )
    adapter.update_card(_card_binding(), "ot-123", _CARD_DATA)
    tokens = [
        call["headers"]["x-acs-dingtalk-access-token"]
        for call in client.calls_to("card/instances")
        if call["method"] == "PUT"
    ]
    assert tokens == ["stale-token", "fresh-token"]


def test_dingtalk_stream_card_puts_streaming_api():
    adapter, client = _card_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "card/streaming": [_Response(200)]}
    )
    adapter.stream_card(_card_binding(), "ot-123", "msgContent", "⏳ 判断意图 退款")
    calls = client.calls_to("card/streaming")
    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/card/streaming")
    assert call["headers"]["x-acs-dingtalk-access-token"] == "token-1"
    body = call["body"]
    assert body["outTrackId"] == "ot-123"
    assert body["key"] == "msgContent"
    assert body["content"] == "⏳ 判断意图 退款"
    assert body["isFull"] is True
    assert body["isFinalize"] is False
    assert body["isError"] is False
    # guid 每次唯一，驱动一次渲染
    assert isinstance(body["guid"], str) and body["guid"]


def test_dingtalk_stream_card_finalize_and_error_flags():
    adapter, client = _card_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "card/streaming": [_Response(200)]}
    )
    adapter.stream_card(
        _card_binding(), "ot-123", "msgContent", "❌ 失败内容", finalize=True, failed=True
    )
    body = client.calls_to("card/streaming")[0]["body"]
    assert body["isFinalize"] is True
    assert body["isError"] is True


def test_dingtalk_stream_card_rejects_invalid_input():
    adapter, _client = _card_adapter(
        {"oauth2/accessToken": _token_route("token-1"), "card/streaming": [_Response(200)]}
    )
    with pytest.raises(DingTalkPermanentError, match="实例标识或内容槽位"):
        adapter.stream_card(_card_binding(), "", "msgContent", "x")
    with pytest.raises(DingTalkPermanentError, match="实例标识或内容槽位"):
        adapter.stream_card(_card_binding(), "ot-123", "", "x")


def test_dingtalk_stream_card_403_mentions_streaming_permission():
    """流式接口 403 应提示开通 Card.Streaming.Write（与实例写权限区分）。"""
    adapter, _client = _card_adapter(
        {
            "oauth2/accessToken": _token_route("token-1"),
            "card/streaming": [
                _Response(403, {"code": "Forbidden.AccessDenied", "message": "no permission"})
            ],
        }
    )
    with pytest.raises(DingTalkPermanentError) as exc_info:
        adapter.stream_card(_card_binding(), "ot-123", "msgContent", "x")
    message = str(exc_info.value)
    assert "Card.Streaming.Write" in message
    assert "Card.Instance.Write" not in message


def test_dingtalk_stream_card_refreshes_token_once_on_401():
    adapter, client = _card_adapter(
        {
            "oauth2/accessToken": _token_route("stale-token", "fresh-token"),
            "card/streaming": [_Response(401), _Response(200)],
        }
    )
    adapter.stream_card(_card_binding(), "ot-123", "msgContent", "x")
    tokens = [
        call["headers"]["x-acs-dingtalk-access-token"]
        for call in client.calls_to("card/streaming")
    ]
    assert tokens == ["stale-token", "fresh-token"]


