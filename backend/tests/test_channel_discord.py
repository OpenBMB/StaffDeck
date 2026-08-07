from __future__ import annotations

import asyncio
import threading
import time

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.adapters.discord import (
    DISCORD_API_BASE,
    DiscordAdapter,
    DiscordPermanentError,
    DiscordStreamManager,
    DiscordTransientError,
    normalize_discord_message,
    validate_discord_credentials,
)
from app.channels.crypto import encrypt_channel_secret
from app.channels.service_discord_inbox import (
    discord_account_key,
    stage_discord_inbound,
)
from app.channels.service_durable_inbox import StageDisposition
from app.db.models import ChannelBinding, ChannelInboundEvent, Tenant


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
        "id": "msg-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "author_id": "user-1",
        "author_name": "Alice",
        "content": "hello",
        "mentions": ["bot-1"],
        "bot_user_id": "bot-1",
        "is_group": True,
    }
    value.update(overrides)
    return value


def test_normalize_discord_dm_and_group():
    dm = normalize_discord_message(
        _raw(guild_id="", is_group=False, content="hi bot", mentions=[])
    )
    assert dm is not None
    assert dm.channel == "discord"
    assert dm.event_id == "msg-1"
    assert dm.from_user_id == "user-1"
    assert dm.to_user_id == "bot-1"
    assert dm.session_id == "dm:user-1"
    assert dm.group_id == ""
    assert dm.is_group is False
    assert dm.text == "hi bot"
    assert dm.sender_name == "Alice"

    group = normalize_discord_message(_raw())
    assert group is not None
    assert group.is_group is True
    assert group.group_id == "guild-1"
    assert group.session_id == "channel-1"


def test_normalize_discord_filters_own_and_invalid():
    assert normalize_discord_message(_raw(author_id="bot-1")) is None
    assert normalize_discord_message(_raw(content="  ")) is None
    assert normalize_discord_message(_raw(id="")) is None
    assert normalize_discord_message(None) is None
    # 群聊未 @bot 的消息不响应。
    assert normalize_discord_message(_raw(mentions=[])) is None


def test_normalize_discord_strips_bot_mention_in_group():
    group = normalize_discord_message(_raw(content="<@!123456789> hello"))
    assert group is not None
    assert group.text == "hello"
    # 没有提到的内容保留原样。
    plain = normalize_discord_message(_raw(content="hello <@!123456789>"))
    assert plain is not None
    assert plain.text == "hello <@!123456789>"


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

    def get(self, url, headers=None, **_kwargs):
        self.calls.append({"url": url, "headers": headers or {}})
        for fragment, queue in self.routes.items():
            if fragment in url:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"未预期的请求地址 {url}")

    def calls_to(self, fragment):
        return [call for call in self.calls if fragment in call["url"]]


def _binding(**overrides):
    values = {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "channel": "discord",
        "status": "active",
        "credentials_enc": encrypt_channel_secret("secret"),
        "config_json": {"bot_id": "bot-1"},
        "external_account_key": discord_account_key("bot-1"),
        "config_revision": 1,
    }
    values.update(overrides)
    return ChannelBinding(**values)


def test_discord_send_posts_to_channel_with_bot_auth():
    class Client:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, json=None, headers=None, **_kwargs):
            self.calls.append({"url": url, "body": json, "headers": headers or {}})
            return _Response(200)

    client = Client()
    adapter = DiscordAdapter(client_factory=lambda: client)
    adapter.send(
        _binding(),
        {"channel_id": "channel-1"},
        "hello",
        idempotency_key="delivery-1",
    )
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == f"{DISCORD_API_BASE}/channels/channel-1/messages"
    assert call["headers"]["Authorization"] == "Bot secret"
    assert call["body"] == {"content": "hello"}


def test_discord_send_splits_long_text():
    class Client:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, json=None, headers=None, **_kwargs):
            self.calls.append(json)
            return _Response(200)

    client = Client()
    adapter = DiscordAdapter(client_factory=lambda: client)
    adapter.send(
        _binding(),
        {"channel_id": "channel-1"},
        "x" * 2500,
        idempotency_key="delivery-1",
    )
    assert len(client.calls) == 2
    assert sum(len(call["content"]) for call in client.calls) == 2500


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (500, DiscordTransientError),
        (429, DiscordTransientError),
        (401, DiscordPermanentError),
        (403, DiscordPermanentError),
    ],
)
def test_discord_send_error_classification(status, expected):
    class Client:
        def post(self, url, json=None, headers=None, **_kwargs):
            return _Response(status)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    adapter = DiscordAdapter(client_factory=lambda: Client())
    with pytest.raises(expected):
        adapter.send(_binding(), {"channel_id": "channel-1"}, "hello")


def test_discord_send_rejects_missing_channel():
    adapter = DiscordAdapter()
    with pytest.raises(DiscordPermanentError):
        adapter.send(_binding(), {}, "hello")


def test_validate_discord_credentials_ok():
    client = _RoutingClient(
        {"users/@me": [_Response(200, {"id": "bot-1", "username": "MyBot"})]}
    )
    info = validate_discord_credentials("secret", client_factory=lambda: client)
    assert info == {"bot_id": "bot-1", "bot_name": "MyBot"}
    call = client.calls_to("users/@me")[0]
    assert call["headers"]["Authorization"] == "Bot secret"


def test_validate_discord_credentials_errors():
    bad_token = validate_discord_credentials("", client_factory=lambda: _RoutingClient({}))
    assert bad_token is None
    with pytest.raises(DiscordPermanentError):
        validate_discord_credentials(
            "secret",
            client_factory=lambda: _RoutingClient({"users/@me": [_Response(401)]}),
        )
    with pytest.raises(DiscordTransientError):
        validate_discord_credentials(
            "secret",
            client_factory=lambda: _RoutingClient({"users/@me": [_Response(500)]}),
        )


def test_stage_discord_inbound_is_deduplicated():
    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        binding = _binding()
        db.add(binding)
        db.commit()
        binding_id = binding.id
    inbound = normalize_discord_message(_raw(), account_scope="")
    assert inbound is not None
    first = stage_discord_inbound(
        db_engine=db_engine,
        binding_id=binding_id,
        expected_revision=1,
        bot_id="bot-1",
        inbound=inbound,
    )
    second = stage_discord_inbound(
        db_engine=db_engine,
        binding_id=binding_id,
        expected_revision=1,
        bot_id="bot-1",
        inbound=inbound,
    )
    assert first.disposition is StageDisposition.STAGED
    assert second.disposition is StageDisposition.DUPLICATE
    with Session(db_engine) as db:
        events = db.exec(select(ChannelInboundEvent)).all()
        assert len(events) == 1
        assert events[0].target_json["to_user_id"] == "guild-1"  # 群聊 to_user_id=conv_key
        assert events[0].target_json["channel_id"] == "channel-1"


def test_stage_discord_inbound_fence_rejections():
    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        binding = _binding()
        db.add(binding)
        db.commit()
        binding_id = binding.id
    inbound = normalize_discord_message(_raw(), account_scope="")
    assert inbound is not None

    missing = stage_discord_inbound(
        db_engine=db_engine, binding_id="missing", expected_revision=1,
        bot_id="bot-1", inbound=inbound,
    )
    assert missing.disposition is StageDisposition.SECURITY_DROP

    wrong_channel = stage_discord_inbound(
        db_engine=db_engine, binding_id=binding_id, expected_revision=1,
        bot_id="bot-2", inbound=inbound,
    )
    assert wrong_channel.disposition is StageDisposition.SECURITY_DROP

    wrong_revision = stage_discord_inbound(
        db_engine=db_engine, binding_id=binding_id, expected_revision=99,
        bot_id="bot-1", inbound=inbound,
    )
    assert wrong_revision.disposition is StageDisposition.SECURITY_DROP


class _FakeDiscordClient:
    """最小 discord.Client 替身:start() 阻塞直到 close(),支持挂载 on_ready。"""

    def __init__(self):
        self._closed = threading.Event()
        self._handlers = {}
        self.on_ready_fired = threading.Event()

    def event(self, coro):
        self._handlers[coro.__name__.lstrip("_")] = coro
        return coro

    async def start(self, token: str) -> None:
        if "on_ready" in self._handlers:
            await self._handlers["on_ready"]()
            self.on_ready_fired.set()
        await asyncio.to_thread(self._closed.wait)

    async def close(self) -> None:
        self._closed.set()


def _stream_manager(db_engine):
    clients = []

    def factory(token, on_message):
        client = _FakeDiscordClient()
        clients.append(client)
        return client

    manager = DiscordStreamManager(db_engine=db_engine, client_factory=factory)
    manager._test_clients = clients
    return manager


def _wait_until(predicate, timeout_seconds: float = 3.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_discord_stream_manager_stop_terminates_gateway_thread():
    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        db.add(_binding(id="chan-1"))
        db.commit()

    manager = _stream_manager(db_engine)
    manager.ensure_binding("chan-1")
    assert _wait_until(lambda: bool(manager._test_clients))
    assert manager._threads["chan-1"].is_alive()

    manager.stop_binding("chan-1")
    assert manager.wait_binding_stopped("chan-1", timeout_seconds=3.0)
    assert manager._test_clients[0]._closed.is_set()
    assert "chan-1" not in manager._threads


def test_discord_stream_manager_connected_only_after_ready():
    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        db.add(_binding(id="chan-1"))
        db.commit()

    manager = _stream_manager(db_engine)
    manager.ensure_binding("chan-1")
    assert _wait_until(lambda: bool(manager._test_clients))
    assert _wait_until(lambda: manager._test_clients[0].on_ready_fired.is_set())
    with Session(db_engine) as db:
        binding = db.get(ChannelBinding, "chan-1")
        assert binding.connected is True

    manager.stop_binding("chan-1")
    assert manager.wait_binding_stopped("chan-1", timeout_seconds=3.0)
    with Session(db_engine) as db:
        binding = db.get(ChannelBinding, "chan-1")
        assert binding.connected is False


def test_discord_stream_manager_start_is_idempotent():
    manager = _stream_manager(_engine())
    manager.start()
    first = manager._reconcile_thread
    manager.start()
    assert manager._reconcile_thread is first
    manager.stop(timeout_seconds=2.0)


def test_discord_wait_binding_stopped_accepts_positional_timeout():
    """__init__.py wait_binding_ingress_stopped 以位置参数调用 wait_binding_stopped(binding_id, timeout_seconds)。"""
    manager = _stream_manager(_engine())
    # 位置参数形式（与 channels/__init__.py:230 一致）不应抛 TypeError
    result = manager.wait_binding_stopped("chan-none", 0.1)
    assert result is True


def test_discord_hub_wait_binding_ingress_stopped_discord_branch():
    """hub 层 wait_binding_ingress_stopped("discord", ...) 以位置参数走 discord 分支（__init__.py:230）。"""
    from app.channels import wait_binding_ingress_stopped

    result = wait_binding_ingress_stopped("discord", "chan-none", 0.1)
    assert result is True
