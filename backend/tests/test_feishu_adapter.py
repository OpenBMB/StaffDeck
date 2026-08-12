from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.channels.adapters.feishu import (
    FeishuAdapter,
    FeishuPermanentError,
    FeishuTransientError,
    FeishuTokenProvider,
    validate_feishu_credentials,
)
from app.channels.crypto import encrypt_channel_secret
from app.channels.service_outbox import run_delivery_daemon
from app.channels.feishu_runtime import _build_event_dispatcher, _normalize_event
from app.db.models import ChannelBinding, ChannelDelivery, Tenant, utc_now


class FakeClient:
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, **kwargs):
        return self.handler(url, kwargs)

    def get(self, url, **kwargs):
        return self.handler(url, {**kwargs, "_method": "GET"})

    def delete(self, url, **kwargs):
        return self.handler(url, {**kwargs, "_method": "DELETE"})


def _response(status: int, payload: dict, url: str) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", url))


def _binding() -> ChannelBinding:
    return ChannelBinding(
        id="chan_feishu",
        tenant_id="tenant_a",
        agent_id="agent_a",
        channel="feishu",
        status="active",
        config_json={"app_id": "cli_app"},
        credentials_enc=encrypt_channel_secret("secret-value"),
        external_account_key="feishu:app:7:cli_app",
        provider_tenant_key="tenant_key",
        config_revision=3,
    )


def test_reply_uses_cached_token_and_stable_per_chunk_uuid() -> None:
    calls = []

    def handler(url, kwargs):
        calls.append((url, kwargs))
        if "/auth/" in url:
            return _response(200, {"code": 0, "tenant_access_token": "token-a", "expire": 7200}, url)
        return _response(200, {"code": 0, "msg": "success"}, url)

    def factory():
        return FakeClient(handler)
    adapter = FeishuAdapter(
        token_provider=FeishuTokenProvider(client_factory=factory),
        client_factory=factory,
    )
    target = {"message_id": "om_source", "reply_in_thread": True}

    adapter.send(_binding(), target, "reply", idempotency_key="delivery-a")
    adapter.send(_binding(), target, "reply", idempotency_key="delivery-a")

    token_calls = [call for call in calls if "/auth/" in call[0]]
    send_calls = [call for call in calls if "/messages/om_source/reply" in call[0]]
    assert len(token_calls) == 1
    assert len(send_calls) == 2
    assert send_calls[0][1]["json"]["uuid"] == send_calls[1][1]["json"]["uuid"]
    assert len(send_calls[0][1]["json"]["uuid"]) == 40
    assert send_calls[0][1]["json"]["reply_in_thread"] is True
    assert send_calls[0][1]["headers"] == {"Authorization": "Bearer token-a"}


def test_create_message_uses_receive_id_query() -> None:
    calls = []

    def handler(url, kwargs):
        calls.append((url, kwargs))
        if "/auth/" in url:
            return _response(200, {"code": 0, "tenant_access_token": "token-a", "expire": 7200}, url)
        return _response(200, {"code": 0}, url)

    def factory():
        return FakeClient(handler)
    adapter = FeishuAdapter(client_factory=factory)
    adapter.send(
        _binding(),
        {"receive_id": "ou_user", "receive_id_type": "open_id"},
        "hello",
        idempotency_key="delivery-create",
    )
    send = next(call for call in calls if call[0].endswith("/im/v1/messages"))
    assert send[1]["params"] == {"receive_id_type": "open_id"}
    assert send[1]["json"]["receive_id"] == "ou_user"


def test_reaction_add_and_remove_use_message_reaction_api() -> None:
    calls = []

    def handler(url, kwargs):
        if "/auth/" in url:
            return _response(200, {"code": 0, "tenant_access_token": "token", "expire": 7200}, url)
        calls.append((url, kwargs))
        if kwargs.get("_method") == "GET":
            return _response(
                200,
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "reaction_id": "rx_existing",
                                "operator": {
                                    "operator_type": "app",
                                    "operator_id": "cli_app",
                                },
                                "reaction_type": {"emoji_type": "Get"},
                            }
                        ],
                        "has_more": False,
                    },
                },
                url,
            )
        if kwargs.get("_method") == "DELETE":
            return _response(200, {"code": 0}, url)
        return _response(200, {"code": 0, "data": {"reaction_id": "rx_123"}}, url)

    adapter = FeishuAdapter(client_factory=lambda: FakeClient(handler))
    target = {"message_id": "om_source"}
    existing_reaction_id = adapter.find_own_reaction(_binding(), target, "Get")
    reaction_id = adapter.add_reaction(_binding(), target, "Get")
    adapter.remove_reaction(_binding(), target, reaction_id)

    assert existing_reaction_id == "rx_existing"
    assert reaction_id == "rx_123"
    assert calls[0][0].endswith("/im/v1/messages/om_source/reactions")
    assert calls[0][1]["params"]["reaction_type"] == "Get"
    assert calls[1][1]["json"] == {"reaction_type": {"emoji_type": "Get"}}
    assert calls[2][0].endswith("/im/v1/messages/om_source/reactions/rx_123")
    assert calls[2][1]["_method"] == "DELETE"


def test_reaction_remove_treats_404_as_idempotent_success() -> None:
    def handler(url, kwargs):
        if "/auth/" in url:
            return _response(200, {"code": 0, "tenant_access_token": "token", "expire": 7200}, url)
        return _response(404, {"code": 231003}, url)

    adapter = FeishuAdapter(client_factory=lambda: FakeClient(handler))
    adapter.remove_reaction(_binding(), {"message_id": "om_source"}, "rx_missing")


def test_401_refreshes_token_exactly_once() -> None:
    tokens = iter(["token-old", "token-new"])
    send_count = 0

    def handler(url, kwargs):
        nonlocal send_count
        if "/auth/" in url:
            return _response(
                200,
                {"code": 0, "tenant_access_token": next(tokens), "expire": 7200},
                url,
            )
        send_count += 1
        if send_count == 1:
            return _response(401, {"code": 99991663}, url)
        assert kwargs["headers"]["Authorization"] == "Bearer token-new"
        return _response(200, {"code": 0}, url)

    def factory():
        return FakeClient(handler)
    adapter = FeishuAdapter(client_factory=factory)
    adapter.send(_binding(), {"message_id": "om_source"}, "ok", idempotency_key="delivery")
    assert send_count == 2


def test_second_token_invalid_business_response_is_permanent() -> None:
    token_counter = 0

    def handler(url, _kwargs):
        nonlocal token_counter
        if "/auth/" in url:
            token_counter += 1
            return _response(
                200,
                {"code": 0, "tenant_access_token": f"token-{token_counter}", "expire": 7200},
                url,
            )
        return _response(200, {"code": 99991663}, url)

    adapter = FeishuAdapter(client_factory=lambda: FakeClient(handler))
    with pytest.raises(FeishuPermanentError, match="刷新后仍无效"):
        adapter.send(_binding(), {"message_id": "om_source"}, "x", idempotency_key="d")
    assert token_counter == 2


@pytest.mark.parametrize("failure_url", ["token", "bot"])
def test_credential_validation_classifies_provider_outage_as_transient(failure_url) -> None:
    def handler(url, _kwargs):
        if "/auth/" in url:
            if failure_url == "token":
                return _response(429, {"code": 1}, url)
            return _response(200, {"code": 0, "tenant_access_token": "token", "expire": 7200}, url)
        return _response(503, {"code": 1}, url)

    with pytest.raises(FeishuTransientError, match="暂时不可用"):
        validate_feishu_credentials(
            "cli_app",
            "secret",
            client_factory=lambda: FakeClient(handler),
        )


def test_http_200_business_error_is_permanent() -> None:
    def handler(url, _kwargs):
        if "/auth/" in url:
            return _response(200, {"code": 0, "tenant_access_token": "token", "expire": 7200}, url)
        return _response(200, {"code": 230001, "msg": "invalid target"}, url)

    adapter = FeishuAdapter(client_factory=lambda: FakeClient(handler))
    with pytest.raises(FeishuPermanentError, match="code=230001"):
        adapter.send(_binding(), {"message_id": "om_source"}, "x", idempotency_key="d")


def test_token_fetch_is_single_flight_for_same_binding() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def handler(url, _kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2.0)
        return _response(200, {"code": 0, "tenant_access_token": "token", "expire": 7200}, url)

    provider = FeishuTokenProvider(client_factory=lambda: FakeClient(handler))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(provider.get, _binding())
        assert entered.wait(timeout=2.0)
        second = pool.submit(provider.get, _binding())
        release.set()
        assert first.result() == "token"
        assert second.result() == "token"
    assert calls == 1


def test_long_message_chunks_use_distinct_stable_uuids() -> None:
    calls = []

    def handler(url, kwargs):
        if "/auth/" in url:
            return _response(200, {"code": 0, "tenant_access_token": "token", "expire": 7200}, url)
        calls.append(kwargs["json"]["uuid"])
        return _response(200, {"code": 0}, url)

    adapter = FeishuAdapter(client_factory=lambda: FakeClient(handler))
    target = {"message_id": "om_source"}
    adapter.send(_binding(), target, "x" * 2001, idempotency_key="long-delivery")
    first_attempt = list(calls)
    adapter.send(_binding(), target, "x" * 2001, idempotency_key="long-delivery")
    assert len(first_attempt) == 2
    assert first_attempt[0] != first_attempt[1]
    assert calls[2:] == first_attempt


def test_empty_text_is_permanent_error() -> None:
    adapter = FeishuAdapter(client_factory=lambda: FakeClient(lambda *_args: None))
    with pytest.raises(FeishuPermanentError, match="不能为空"):
        adapter.send(_binding(), {"message_id": "om_source"}, "  ", idempotency_key="d")


@pytest.mark.parametrize(
    "event",
    [
        SimpleNamespace(header=None, event=None),
        SimpleNamespace(header=SimpleNamespace(), event=SimpleNamespace(message=None, sender=None)),
        SimpleNamespace(
            header=SimpleNamespace(),
            event=SimpleNamespace(
                message=SimpleNamespace(),
                sender=SimpleNamespace(sender_id=None),
            ),
        ),
    ],
)
def test_production_normalizer_ack_drops_missing_nested_objects(event) -> None:
    assert _normalize_event(event, bot_open_id="ou_bot") is None


def _group_event(
    text: str,
    *,
    include_bot_mention: bool = True,
    thread_id: str = "",
    root_id: str = "",
):
    mentions = []
    if include_bot_mention:
        mentions.append(
            SimpleNamespace(
                id=SimpleNamespace(open_id="ou_bot"),
                key="@_user_1",
                mentioned_type="bot",
            )
        )
    message = SimpleNamespace(
        message_id="om_group",
        chat_id="oc_group",
        chat_type="group",
        message_type="text",
        content=json.dumps({"text": text}),
        mentions=mentions,
        thread_id=thread_id,
        root_id=root_id,
    )
    return SimpleNamespace(
        header=SimpleNamespace(app_id="cli_app", tenant_key="tenant_key"),
        event=SimpleNamespace(
            message=message,
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_sender"),
                sender_type="user",
            ),
        ),
    )


def test_group_normalizer_requires_bot_mention_and_preserves_group_target() -> None:
    event = _group_event("@_user_1 hello")

    inbound, target = _normalize_event(event, bot_open_id="ou_bot")

    assert inbound.text == "hello"
    assert inbound.is_group is True
    assert inbound.external_conv_id == "feishu_group_oc_group"
    assert target == {
        "message_id": "om_group",
        "reply_in_thread": False,
        "receive_id_type": "chat_id",
        "receive_id": "oc_group",
    }
    assert (
        _normalize_event(
            _group_event("hello", include_bot_mention=False),
            bot_open_id="ou_bot",
        )
        is None
    )
    assert _normalize_event(_group_event("@_user_1"), bot_open_id="ou_bot") is None


def test_ordinary_group_reply_root_id_does_not_create_topic() -> None:
    inbound, target = _normalize_event(
        _group_event("@_user_1 follow up", root_id="om_root"),
        bot_open_id="ou_bot",
    )

    assert inbound.external_conv_id == "feishu_group_oc_group"
    assert target["reply_in_thread"] is False


def test_group_topic_uses_thread_id_for_session_and_reply() -> None:
    inbound, target = _normalize_event(
        _group_event(
            "@_user_1 topic reply",
            thread_id="omt_topic",
            root_id="om_root",
        ),
        bot_open_id="ou_bot",
    )

    assert inbound.external_conv_id == "feishu_group_oc_group:thread:omt_topic"
    assert target["reply_in_thread"] is True


@pytest.mark.parametrize(
    "event_type",
    [
        "im.chat.member.bot.added_v1",
        "im.chat.member.bot.deleted_v1",
        "im.chat.access_event.bot_p2p_chat_entered_v1",
    ],
)
def test_subscribed_lifecycle_events_are_acknowledged_as_noop(event_type: str) -> None:
    from lark_channel import EventDispatcherHandler

    dispatcher = _build_event_dispatcher(EventDispatcherHandler, lambda _event: None)
    payload = json.dumps(
        {
            "schema": "2.0",
            "header": {"event_type": event_type},
            "event": {},
        }
    ).encode()

    assert dispatcher._do_without_validation(payload) is None


def test_old_sending_delivery_is_not_replayed_outside_uuid_window(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'outbox.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="A"))
        binding = _binding()
        db.add(binding)
        delivery = ChannelDelivery(
            tenant_id="tenant_a",
            binding_id=binding.id,
            session_id="session_a",
            target_json={"message_id": "om_source"},
            kind="reply",
            text="maybe sent",
            status="sending",
            attempts=1,
            idempotency_key="delivery-old",
            first_attempt_at=utc_now() - timedelta(minutes=56),
        )
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id

    run_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "failed"
        assert delivery.last_error == "remote_state_unknown"
        assert delivery.next_attempt_at is None


def test_old_pending_retry_is_not_replayed_outside_uuid_window(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'pending-outbox.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="A"))
        binding = _binding()
        db.add(binding)
        delivery = ChannelDelivery(
            tenant_id="tenant_a",
            binding_id=binding.id,
            session_id="session_a",
            target_json={"message_id": "om_source"},
            kind="reply",
            text="partially sent",
            status="pending",
            attempts=1,
            next_attempt_at=utc_now(),
            idempotency_key="delivery-pending-old",
            first_attempt_at=utc_now() - timedelta(minutes=56),
        )
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "failed"
        assert delivery.last_error == "remote_state_unknown"


def _p2p_event(*, message_type: str, content: dict, chat_type: str = "p2p"):
    """构造飞书 P2P/群消息 event(用于 normalize image/file 测试)。"""
    message = SimpleNamespace(
        message_id="om_normalize",
        chat_id="oc_chat" if chat_type == "group" else "",
        chat_type=chat_type,
        message_type=message_type,
        content=json.dumps(content),
        mentions=[],
        thread_id="",
        root_id="",
    )
    return SimpleNamespace(
        header=SimpleNamespace(app_id="cli_app", tenant_key="tenant_key"),
        event=SimpleNamespace(
            message=message,
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_sender"),
                sender_type="user",
            ),
        ),
    )


def _group_attachment_event(message_type: str, content: dict) -> object:
    message = SimpleNamespace(
        message_id="om_group_att",
        chat_id="oc_group",
        chat_type="group",
        message_type=message_type,
        content=json.dumps(content),
        mentions=[
            SimpleNamespace(
                id=SimpleNamespace(open_id="ou_bot"),
                key="@_user_1",
                mentioned_type="bot",
            )
        ],
        thread_id="",
        root_id="",
    )
    return SimpleNamespace(
        header=SimpleNamespace(app_id="cli_app", tenant_key="tenant_key"),
        event=SimpleNamespace(
            message=message,
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_sender"),
                sender_type="user",
            ),
        ),
    )


def test_normalize_image_message_extracts_attachment() -> None:
    from app.channels.adapters.base import ChannelInboundAttachment

    event = _p2p_event(
        message_type="image",
        content={"image_key": "img_v3_001"},
    )
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, _target = result
    # 图片消息无文本,但附件存在所以不再被丢弃
    assert inbound.text == ""
    assert len(inbound.attachments) == 1
    att = inbound.attachments[0]
    assert isinstance(att, ChannelInboundAttachment)
    assert att.media_id == "img_v3_001"
    assert att.kind == "image"
    assert att.filename == "img_v3_001"  # 不在 normalize 阶段硬编码扩展名
    assert att.content_type == ""  # 下载后由 _resolve_content_type 推断
    assert att.download_params == {
        "file_key": "img_v3_001",
        "type": "image",
        "message_id": "om_normalize",
    }


def test_normalize_file_message_extracts_attachment_with_filename() -> None:
    event = _p2p_event(
        message_type="file",
        content={"file_key": "file_v3_001", "file_name": "report.pdf"},
    )
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, _target = result
    assert inbound.text == ""
    assert len(inbound.attachments) == 1
    att = inbound.attachments[0]
    assert att.media_id == "file_v3_001"
    assert att.kind == "file"
    assert att.filename == "report.pdf"
    assert att.content_type == ""
    assert att.download_params == {
        "file_key": "file_v3_001",
        "type": "file",
        "message_id": "om_normalize",
    }


def test_normalize_text_message_has_empty_attachments() -> None:
    """纯文本消息仍正常工作,attachments 为空列表。"""
    event = _p2p_event(message_type="text", content={"text": "hello"})
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, _ = result
    assert inbound.text == "hello"
    assert inbound.attachments == []


def test_normalize_image_without_image_key_returns_none() -> None:
    """image 消息但 content 缺 image_key 时返回 None(无文本也无附件)。"""
    event = _p2p_event(message_type="image", content={})
    assert _normalize_event(event, bot_open_id="ou_bot") is None


def test_normalize_group_attachment_without_mention_is_accepted() -> None:
    """群聊 image/file 消息天然不携带 mentions,无需 @ 机器人即可通过。"""
    # 不带 @ 机器人 → 仍接受(image 消息无法携带 mentions)
    event_no_mention = _p2p_event(
        message_type="image",
        content={"image_key": "img_v3_002"},
        chat_type="group",
    )
    result = _normalize_event(event_no_mention, bot_open_id="ou_bot")
    assert result is not None
    inbound, _target = result
    assert inbound.is_group is True
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].media_id == "img_v3_002"

    # 带 @ 机器人的 text+image 也正常
    event = _group_attachment_event("image", {"image_key": "img_v3_003"})
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, target = result
    assert inbound.is_group is True
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].media_id == "img_v3_003"
    assert target["receive_id_type"] == "chat_id"
    assert target["receive_id"] == "oc_group"


def _binary_response(status: int, content: bytes, url: str) -> httpx.Response:
    return httpx.Response(
        status,
        content=content,
        request=httpx.Request("GET", url),
    )


def test_download_media_calls_resources_endpoint_with_bearer_token() -> None:
    calls = []

    def handler(url, kwargs):
        calls.append((url, kwargs))
        if "/auth/" in url:
            return _response(200, {"code": 0, "tenant_access_token": "token-a", "expire": 7200}, url)
        # 附件下载请求
        assert url.endswith("/im/v1/messages/om_msg/resources/img_v3_001")
        assert kwargs["params"] == {"type": "image"}
        assert kwargs["headers"]["Authorization"] == "Bearer token-a"
        return _binary_response(200, b"PNG-bytes", url)

    def factory():
        return FakeClient(handler)

    adapter = FeishuAdapter(client_factory=factory)
    from app.channels.adapters.base import ChannelInboundAttachment

    att = ChannelInboundAttachment(
        media_id="img_v3_001",
        kind="image",
        download_params={
            "file_key": "img_v3_001",
            "type": "image",
            "message_id": "om_msg",
        },
    )
    data = adapter.download_media(_binding(), att)
    assert data == b"PNG-bytes"
    # 确认只调用了一次资源下载
    resource_calls = [c for c in calls if "/resources/" in c[0]]
    assert len(resource_calls) == 1


def test_download_media_missing_params_is_permanent_error() -> None:
    adapter = FeishuAdapter(client_factory=lambda: FakeClient(lambda *_args: None))
    from app.channels.adapters.base import ChannelInboundAttachment

    att = ChannelInboundAttachment(
        media_id="img_v3_001",
        kind="image",
        download_params={"file_key": "img_v3_001"},  # 缺 type 和 message_id
    )
    with pytest.raises(FeishuPermanentError, match="参数缺失"):
        adapter.download_media(_binding(), att)


def test_download_media_refreshes_token_on_401() -> None:
    tokens = iter(["token-old", "token-new"])
    resource_count = 0

    def handler(url, kwargs):
        if "/auth/" in url:
            return _response(
                200,
                {"code": 0, "tenant_access_token": next(tokens), "expire": 7200},
                url,
            )
        nonlocal resource_count
        resource_count += 1
        if resource_count == 1:
            assert kwargs["headers"]["Authorization"] == "Bearer token-old"
            return _binary_response(401, b"", url)
        assert kwargs["headers"]["Authorization"] == "Bearer token-new"
        return _binary_response(200, b"PNG-bytes", url)

    def factory():
        return FakeClient(handler)

    adapter = FeishuAdapter(client_factory=factory)
    from app.channels.adapters.base import ChannelInboundAttachment

    att = ChannelInboundAttachment(
        media_id="img_v3_001",
        kind="image",
        download_params={
            "file_key": "img_v3_001",
            "type": "image",
            "message_id": "om_msg",
        },
    )
    data = adapter.download_media(_binding(), att)
    assert data == b"PNG-bytes"
    assert resource_count == 2


def test_download_media_5xx_is_transient() -> None:
    def handler(url, _kwargs):
        if "/auth/" in url:
            return _response(200, {"code": 0, "tenant_access_token": "token", "expire": 7200}, url)
        return _binary_response(503, b"", url)

    adapter = FeishuAdapter(client_factory=lambda: FakeClient(handler))
    from app.channels.adapters.base import ChannelInboundAttachment

    att = ChannelInboundAttachment(
        media_id="img_v3_001",
        kind="image",
        download_params={
            "file_key": "img_v3_001",
            "type": "image",
            "message_id": "om_msg",
        },
    )
    with pytest.raises(FeishuTransientError, match="暂时不可用"):
        adapter.download_media(_binding(), att)


def _group_post_event(content: dict, mentions: list | None = None) -> object:
    """构造飞书群聊 post(富文本)消息 event。"""
    message = SimpleNamespace(
        message_id="om_post",
        chat_id="oc_group",
        chat_type="group",
        message_type="post",
        content=json.dumps(content),
        mentions=mentions or [],
        thread_id="",
        root_id="",
    )
    return SimpleNamespace(
        header=SimpleNamespace(app_id="cli_app", tenant_key="tenant_key"),
        event=SimpleNamespace(
            message=message,
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_sender"),
                sender_type="user",
            ),
        ),
    )


def test_normalize_post_message_extracts_image_and_text() -> None:
    """群聊 post 消息(图片+@机器人+文本)应提取图片附件和文本,不再被丢弃。"""
    from app.channels.adapters.base import ChannelInboundAttachment

    content = {
        "zh_cn": {
            "title": "",
            "content": [
                [{"tag": "img", "image_key": "img_v3_post_1"}],
                [
                    {"tag": "at", "user_id": "ou_bot"},
                    {"tag": "text", "text": " 看这张图"},
                ],
            ],
        }
    }
    mentions = [
        SimpleNamespace(
            id=SimpleNamespace(open_id="ou_bot"),
            key="@_user_1",
            mentioned_type="bot",
        )
    ]
    event = _group_post_event(content, mentions=mentions)
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, target = result
    assert inbound.is_group is True
    assert len(inbound.attachments) == 1
    att = inbound.attachments[0]
    assert isinstance(att, ChannelInboundAttachment)
    assert att.media_id == "img_v3_post_1"
    assert att.kind == "image"
    assert att.download_params == {
        "file_key": "img_v3_post_1",
        "type": "image",
        "message_id": "om_post",
    }
    assert inbound.text == "看这张图"
    assert target["receive_id_type"] == "chat_id"
    assert target["receive_id"] == "oc_group"


def test_normalize_post_message_only_image_without_mention_is_accepted() -> None:
    """群聊 post 消息仅含图片且未 @机器人,应被接受(image 消息天然不携带 mentions)。"""
    content = {
        "zh_cn": {
            "content": [[{"tag": "img", "image_key": "img_v3_post_2"}]],
        }
    }
    event = _group_post_event(content)
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, _target = result
    assert inbound.is_group is True
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].media_id == "img_v3_post_2"
    assert inbound.text == ""


def test_normalize_post_message_multiple_images() -> None:
    """post 消息含多张图片时应全部提取。"""
    content = {
        "zh_cn": {
            "content": [
                [
                    {"tag": "img", "image_key": "img_v3_multi_1"},
                    {"tag": "img", "image_key": "img_v3_multi_2"},
                ],
                [{"tag": "text", "text": "两张图"}],
            ],
        }
    }
    mentions = [
        SimpleNamespace(
            id=SimpleNamespace(open_id="ou_bot"),
            key="@_user_1",
            mentioned_type="bot",
        )
    ]
    event = _group_post_event(content, mentions=mentions)
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, _target = result
    assert len(inbound.attachments) == 2
    assert inbound.attachments[0].media_id == "img_v3_multi_1"
    assert inbound.attachments[1].media_id == "img_v3_multi_2"
    assert inbound.text == "两张图"


def test_normalize_post_message_without_image_or_text_returns_none() -> None:
    """post 消息既无图片也无文本时返回 None。"""
    content = {
        "zh_cn": {
            "content": [[{"tag": "at", "user_id": "ou_bot"}]],
        }
    }
    mentions = [
        SimpleNamespace(
            id=SimpleNamespace(open_id="ou_bot"),
            key="@_user_1",
            mentioned_type="bot",
        )
    ]
    event = _group_post_event(content, mentions=mentions)
    assert _normalize_event(event, bot_open_id="ou_bot") is None


def test_normalize_post_message_en_us_locale() -> None:
    """post 消息使用 en_us locale 也应正确解析。"""
    content = {
        "en_us": {
            "content": [
                [{"tag": "img", "image_key": "img_v3_en"}],
                [{"tag": "text", "text": "check this"}],
            ],
        }
    }
    mentions = [
        SimpleNamespace(
            id=SimpleNamespace(open_id="ou_bot"),
            key="@_user_1",
            mentioned_type="bot",
        )
    ]
    event = _group_post_event(content, mentions=mentions)
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, _target = result
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].media_id == "img_v3_en"
    assert inbound.text == "check this"


def test_normalize_post_message_without_locale_wrapper() -> None:
    """post 消息 content 不带 locale 包裹(zh_cn/en_us)时也应正确解析。

    部分飞书客户端在群聊中发送图片+@机器人时,content 结构为:
    {"title":"","content":[[...]],"content_v2":[[...]]}
    没有 zh_cn 外层包裹。
    """
    content = {
        "title": "",
        "content": [
            [{"tag": "img", "image_key": "img_v3_nolocale"}],
            [
                {"tag": "at", "user_id": "@_user_1", "user_name": "StaffDeck渠道接入测试机器人"},
                {"tag": "text", "text": " 查询假期余额"},
            ],
        ],
        "content_v2": [
            [{"tag": "img", "image_key": "img_v3_nolocale"}],
            [
                {"tag": "at", "user_id": "@_user_1"},
                {"tag": "text", "text": " 查询假期余额"},
            ],
        ],
    }
    mentions = [
        SimpleNamespace(
            id=SimpleNamespace(open_id="ou_bot"),
            key="@_user_1",
            mentioned_type="bot",
        )
    ]
    event = _group_post_event(content, mentions=mentions)
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, _target = result
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].media_id == "img_v3_nolocale"
    assert inbound.text == "查询假期余额"
