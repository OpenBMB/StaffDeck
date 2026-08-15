"""Discord 渠道 8 项功能扩展的适配器侧测试(波 2-A)。

覆盖:typing(功能6)、白名单(功能5)、线程(功能2)、回填(功能4)、
富媒体(功能8)、原生斜杠命令(功能1)、语音能力声明(功能7)。
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.adapters.base import (
    ChannelCapability,
    evaluate_allowlist,
)
from app.channels.adapters.discord import (
    DISCORD_API_BASE,
    DiscordAdapter,
    DiscordPermanentError,
    DiscordStreamManager,
    DiscordTransientError,
    normalize_discord_message,
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


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload

    def json(self):
        return self._payload

    @property
    def content(self):
        return self._payload if isinstance(self._payload, bytes) else json.dumps(self._payload).encode()


class _RoutingClient:
    """按 URL 片段路由的假 httpx client；每个队列的最后一项会被重复返回。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, json=None, headers=None, files=None, data=None, **_kwargs):
        self.calls.append(
            {"url": url, "body": json, "headers": headers or {}, "files": files, "data": data}
        )
        for fragment, queue in self.routes.items():
            if fragment in url:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"未预期的请求地址 {url}")

    def get(self, url, params=None, headers=None, **_kwargs):
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
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


def _adapter(client):
    return DiscordAdapter(client_factory=lambda: client)


# ---------------------------------------------------------------- 功能6 typing


def test_send_typing_posts_to_channel():
    client = _RoutingClient({"typing": [_Response(204)]})
    _adapter(client).send_typing(_binding(), {"channel_id": "channel-1"}, 1)
    call = client.calls_to("/typing")[0]
    assert call["url"] == f"{DISCORD_API_BASE}/channels/channel-1/typing"
    assert call["headers"]["Authorization"] == "Bot secret"


def test_send_typing_status_two_is_noop():
    client = _RoutingClient({})
    _adapter(client).send_typing(_binding(), {"channel_id": "channel-1"}, 2)
    assert client.calls == []


def test_send_typing_thread_channel():
    client = _RoutingClient({"typing": [_Response(204)]})
    _adapter(client).send_typing(_binding(), {"thread_id": "thread-9"}, 1)
    call = client.calls_to("/typing")[0]
    assert call["url"] == f"{DISCORD_API_BASE}/channels/thread-9/typing"


def test_send_typing_failure_is_logged_not_raised(caplog):
    client = _RoutingClient({"typing": [_Response(500)]})
    with caplog.at_level("WARNING", logger="app.channels.adapters.discord"):
        _adapter(client).send_typing(_binding(), {"channel_id": "channel-1"}, 1)
    assert "typing 发送失败" in caplog.text


# ---------------------------------------------------------------- 功能5 白名单


@pytest.mark.parametrize(
    ("allowlist", "ctx", "expected"),
    [
        (None, {}, True),
        ({}, {}, True),
        ({"mode": "allow_all"}, {}, True),
        ({"mode": "weird"}, {}, True),
        # deny 命中(纯 id 匹配任意维度)
        ({"deny": ["channel-1"]}, {"channel_id": "channel-1"}, False),
        ({"deny": ["user-1"]}, {"author_id": "user-1"}, False),
        ({"deny": ["guild-1"]}, {"guild_id": "guild-1"}, False),
        # deny 维度前缀
        ({"deny": ["channel:channel-1"]}, {"channel_id": "channel-1"}, False),
        ({"deny": ["user:user-1"]}, {"author_id": "user-1"}, False),
        ({"deny": ["guild:guild-1"]}, {"guild_id": "guild-1"}, False),
        ({"deny": ["channel:other"]}, {"channel_id": "channel-1"}, True),
        # deny 优先于 allow
        (
            {"channel_ids": ["channel-1"], "deny": ["channel:channel-1"]},
            {"channel_id": "channel-1"},
            False,
        ),
        # deny_all:仅 allow 内放行
        ({"mode": "deny_all"}, {"channel_id": "channel-1"}, False),
        (
            {"mode": "deny_all", "channel_ids": ["channel-1"]},
            {"channel_id": "channel-1"},
            True,
        ),
        (
            {"mode": "deny_all", "user_ids": ["user-1"]},
            {"author_id": "user-1"},
            True,
        ),
        (
            {"mode": "deny_all", "guild_ids": ["guild-1"]},
            {"guild_id": "guild-1"},
            True,
        ),
        # allow_all:allow 空=放行,非空=仅列表内
        ({"guild_ids": ["guild-1"]}, {"guild_id": "guild-1"}, True),
        ({"guild_ids": ["guild-2"]}, {"guild_id": "guild-1"}, False),
        ({"user_ids": ["user-1"]}, {"author_id": "user-1"}, True),
        ({"user_ids": ["user-2"]}, {"author_id": "user-1"}, False),
        ({"channel_ids": ["channel-1"]}, {"channel_id": "channel-1"}, True),
        ({"channel_ids": ["channel-2"]}, {"channel_id": "channel-1"}, False),
        # role_ids 首版不参与,不影响判定
        ({"role_ids": ["role-1"]}, {"channel_id": "channel-1"}, True),
    ],
)
def test_evaluate_allowlist_matrix(allowlist, ctx, expected):
    assert evaluate_allowlist(ctx, allowlist) is expected


def test_stage_discord_inbound_allowlist_rejected_is_auditable():
    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        db.add(_binding(id="chan-1", config_json={
            "bot_id": "bot-1",
            "allowlist": {"mode": "allow_all", "channel_ids": ["channel-2"]},
        }))
        db.commit()
    inbound = normalize_discord_message(_raw(), account_scope="")
    assert inbound is not None
    result = stage_discord_inbound(
        db_engine=db_engine, binding_id="chan-1", expected_revision=1,
        bot_id="bot-1", inbound=inbound,
    )
    assert result.disposition is StageDisposition.SECURITY_DROP
    assert result.error_code == "allowlist_denied"
    with Session(db_engine) as db:
        events = db.exec(select(ChannelInboundEvent)).all()
        assert len(events) == 1
        assert events[0].status == "rejected"
        assert events[0].error == "allowlist_denied"


def test_stage_discord_inbound_allowlist_rejected_is_idempotent():
    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        db.add(_binding(id="chan-1", config_json={
            "bot_id": "bot-1",
            "allowlist": {"deny": ["channel:channel-1"]},
        }))
        db.commit()
    inbound = normalize_discord_message(_raw(), account_scope="")
    assert inbound is not None
    for _ in range(2):
        result = stage_discord_inbound(
            db_engine=db_engine, binding_id="chan-1", expected_revision=1,
            bot_id="bot-1", inbound=inbound,
        )
        assert result.disposition is StageDisposition.SECURITY_DROP
    with Session(db_engine) as db:
        events = db.exec(select(ChannelInboundEvent)).all()
        assert len(events) == 1


def test_stage_discord_inbound_allowlist_allows_without_config():
    """无 allowlist 配置(存量 binding)一律放行,兼容现状。"""
    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        db.add(_binding(id="chan-1"))
        db.commit()
    inbound = normalize_discord_message(_raw(), account_scope="")
    assert inbound is not None
    result = stage_discord_inbound(
        db_engine=db_engine, binding_id="chan-1", expected_revision=1,
        bot_id="bot-1", inbound=inbound,
    )
    assert result.disposition is StageDisposition.STAGED


# ---------------------------------------------------------------- 功能2 线程


def test_normalize_discord_thread_session_uses_thread_id():
    inbound = normalize_discord_message(
        _raw(channel_id="thread-9", is_thread=True, thread_id="thread-9", parent_id="channel-1")
    )
    assert inbound is not None
    assert inbound.session_id == "thread-9"
    assert inbound.group_id == "guild-1"


def test_stage_discord_inbound_persists_thread_and_command_fields():
    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        db.add(_binding(id="chan-1"))
        db.commit()
    inbound = normalize_discord_message(
        _raw(
            channel_id="thread-9",
            is_thread=True,
            thread_id="thread-9",
            parent_id="channel-1",
            content="/切换 Alice",
            command="/switch",
            mentions=["bot-1", "user-9"],
        ),
        account_scope="",
    )
    assert inbound is not None
    result = stage_discord_inbound(
        db_engine=db_engine, binding_id="chan-1", expected_revision=1,
        bot_id="bot-1", inbound=inbound,
    )
    assert result.disposition is StageDisposition.STAGED
    with Session(db_engine) as db:
        event = db.get(ChannelInboundEvent, result.event_pk)
        assert event.thread_id == "thread-9"
        assert event.command == "/switch"
        assert json.loads(event.mention_user_ids) == ["user-9"]


def test_stage_discord_inbound_thread_allowlist_uses_parent_channel():
    """线程消息按父频道判定白名单。"""
    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        db.add(_binding(id="chan-1", config_json={
            "bot_id": "bot-1",
            "allowlist": {"channel_ids": ["channel-1"]},
        }))
        db.commit()
    inbound = normalize_discord_message(
        _raw(channel_id="thread-9", is_thread=True, thread_id="thread-9", parent_id="channel-1"),
        account_scope="",
    )
    assert inbound is not None
    result = stage_discord_inbound(
        db_engine=db_engine, binding_id="chan-1", expected_revision=1,
        bot_id="bot-1", inbound=inbound,
    )
    assert result.disposition is StageDisposition.STAGED


def test_send_prefers_target_thread_id():
    client = _RoutingClient({"messages": [_Response(200)]})
    _adapter(client).send(_binding(), {"channel_id": "channel-1", "thread_id": "thread-9"}, "hi")
    call = client.calls[0]
    assert call["url"] == f"{DISCORD_API_BASE}/channels/thread-9/messages"


# ---------------------------------------------------------------- 功能4 回填


def test_fetch_history_returns_normalized_messages():
    history = [
        {"id": "m2", "channel_id": "channel-1", "guild_id": "guild-1",
         "author": {"id": "bot-1", "username": "Bot"}, "content": "bot 说过的话",
         "mentions": [], "attachments": []},
        {"id": "m1", "channel_id": "channel-1", "guild_id": "guild-1",
         "author": {"id": "user-1", "username": "Alice"}, "content": "普通历史消息(未 @bot)",
         "mentions": [], "attachments": []},
    ]
    client = _RoutingClient({"messages": [_Response(200, history)]})
    result = _adapter(client).fetch_history(_binding(), {"channel_id": "channel-1"}, limit=100)
    assert [item.event_id for item in result] == ["m2", "m1"]
    # 回填不跳过 bot 自身消息,且不要求群聊 @bot
    assert result[0].from_user_id == "bot-1"
    assert result[1].text == "普通历史消息(未 @bot)"
    call = client.calls_to("/messages")[0]
    assert call["params"]["limit"] == 100
    assert call["headers"]["Authorization"] == "Bot secret"


def test_fetch_history_clamps_limit_and_passes_cursors():
    client = _RoutingClient({"messages": [_Response(200, [])]})
    _adapter(client).fetch_history(
        _binding(), {"channel_id": "channel-1"},
        before="m-10", after="m-5", limit=500,
    )
    call = client.calls_to("/messages")[0]
    assert call["params"]["limit"] == 100
    assert call["params"]["before"] == "m-10"
    assert call["params"]["after"] == "m-5"
    client2 = _RoutingClient({"messages": [_Response(200, [])]})
    _adapter(client2).fetch_history(_binding(), {"channel_id": "channel-1"}, limit=0)
    assert client2.calls_to("/messages")[0]["params"]["limit"] == 1


def test_fetch_history_error_classification():
    with pytest.raises(DiscordPermanentError):
        _adapter(_RoutingClient({"messages": [_Response(401)]})).fetch_history(
            _binding(), {"channel_id": "channel-1"}
        )
    with pytest.raises(DiscordTransientError):
        _adapter(_RoutingClient({"messages": [_Response(429)]})).fetch_history(
            _binding(), {"channel_id": "channel-1"}
        )


def test_fetch_history_rejects_missing_channel():
    with pytest.raises(DiscordPermanentError):
        _adapter(_RoutingClient({})).fetch_history(_binding(), {})


def test_normalize_include_bot_default_keeps_skipping():
    assert normalize_discord_message(_raw(author_id="bot-1")) is None
    assert normalize_discord_message(_raw(author_id="bot-1"), include_bot=True) is not None


# ---------------------------------------------------------------- 功能8 富媒体


def test_normalize_extracts_attachments():
    inbound = normalize_discord_message(_raw(attachments=[
        {"id": "att-1", "filename": "pic.png", "content_type": "image/png",
         "size": 1024, "url": "https://cdn.discordapp.com/attachments/1"},
        {"id": "att-2", "filename": "doc.pdf", "content_type": "application/pdf",
         "size": 2048, "url": "https://cdn.discordapp.com/attachments/2"},
    ]))
    assert inbound is not None
    assert [att.media_id for att in inbound.attachments] == ["att-1", "att-2"]
    assert inbound.attachments[0].kind == "image"
    assert inbound.attachments[0].filename == "pic.png"
    assert inbound.attachments[0].download_params == {"url": "https://cdn.discordapp.com/attachments/1"}
    assert inbound.attachments[1].kind == "file"


def test_normalize_skips_broken_attachments():
    inbound = normalize_discord_message(_raw(attachments=[
        {"id": "", "url": "https://x/1"},
        {"filename": "no-url.png"},
        "not-a-dict",
    ]))
    assert inbound is not None
    assert inbound.attachments == []


def test_download_media_fetches_url_bytes():
    client = _RoutingClient({})
    client.routes = {"https://cdn.discordapp.com/attachments/1": [_Response(200, b"\x89PNG")]}
    from app.channels.adapters.base import ChannelInboundAttachment

    attachment = ChannelInboundAttachment(
        media_id="att-1", kind="image",
        download_params={"url": "https://cdn.discordapp.com/attachments/1"},
    )
    data = _adapter(client).download_media(_binding(), attachment)
    assert data == b"\x89PNG"
    assert client.calls[0]["url"] == "https://cdn.discordapp.com/attachments/1"
    assert "Authorization" not in client.calls[0]["headers"]


def test_download_media_missing_url():
    from app.channels.adapters.base import ChannelInboundAttachment

    attachment = ChannelInboundAttachment(media_id="att-1", kind="image")
    with pytest.raises(DiscordPermanentError):
        _adapter(_RoutingClient({})).download_media(_binding(), attachment)


def test_send_payload_json_embeds():
    client = _RoutingClient({"messages": [_Response(200)]})
    _adapter(client).send(
        _binding(),
        {"channel_id": "channel-1"},
        "fallback",
        idempotency_key="delivery-1",
        payload_json=json.dumps({"content": "卡片", "embeds": [{"title": "标题", "description": "描述"}]}),
    )
    assert len(client.calls) == 1
    body = client.calls[0]["body"]
    assert body["content"] == "卡片"
    assert body["embeds"] == [{"title": "标题", "description": "描述"}]
    assert body["nonce"]
    assert "fallback" not in body["content"]


def test_send_payload_json_embeds_truncated():
    payload = {
        "embeds": [
            {"title": "t" * 500, "description": "d" * 5000,
             "fields": [{"name": "n" * 500, "value": "v" * 2000}] * 30,
             "footer": {"text": "f" * 3000}, "color": "not-int", "unknown": "x"},
        ]
        * 12
    }
    client = _RoutingClient({"messages": [_Response(200)]})
    _adapter(client).send(_binding(), {"channel_id": "channel-1"}, "", payload_json=json.dumps(payload))
    body = client.calls[0]["body"]
    assert len(body["embeds"]) == 10
    embed = body["embeds"][0]
    assert len(embed["title"]) == 256
    assert len(embed["description"]) == 4096
    assert len(embed["fields"]) == 25
    assert len(embed["fields"][0]["name"]) == 256
    assert len(embed["fields"][0]["value"]) == 1024
    assert len(embed["footer"]["text"]) == 2048
    assert "color" not in embed
    assert "unknown" not in embed


def test_send_payload_json_files_uses_multipart():
    payload = {
        "content": "带文件",
        "files": [{"filename": "a.png", "data": "aGVsbG8=", "content_type": "image/png"}],
    }
    client = _RoutingClient({"messages": [_Response(200)]})
    _adapter(client).send(
        _binding(), {"channel_id": "channel-1"}, "", idempotency_key="delivery-1",
        payload_json=json.dumps(payload),
    )
    call = client.calls[0]
    assert call["files"] == [("files[0]", ("a.png", b"hello", "image/png"))]
    assert "Content-Type" not in call["headers"]
    inner = json.loads(call["data"]["payload_json"])
    assert inner["content"] == "带文件"
    assert inner["nonce"]
    assert "files" not in inner


def test_send_payload_json_files_oversize_is_permanent():
    payload = {
        "files": [{"filename": "big.bin", "data": "x" * (8 * 1024 * 1024 + 1)}],
    }
    with pytest.raises(DiscordPermanentError):
        _adapter(_RoutingClient({})).send(
            _binding(), {"channel_id": "channel-1"}, "", payload_json=json.dumps(payload)
        )


def test_send_payload_json_invalid_falls_back_to_text():
    client = _RoutingClient({"messages": [_Response(200)]})
    _adapter(client).send(
        _binding(), {"channel_id": "channel-1"}, "plain",
        payload_json="not-json{{{",
    )
    assert client.calls[0]["body"]["content"] == "plain"


def test_send_payload_json_empty_embeds_falls_back_to_text():
    client = _RoutingClient({"messages": [_Response(200)]})
    _adapter(client).send(
        _binding(), {"channel_id": "channel-1"}, "plain",
        payload_json=json.dumps({"content": "no embeds here"}),
    )
    assert client.calls[0]["body"]["content"] == "plain"


# ---------------------------------------------------------------- 功能1 斜杠命令


def test_command_text_mapping():
    text = DiscordStreamManager._command_text
    assert text("employee") == "/员工"
    assert text("employee", "Alice") == "/切换 Alice"
    assert text("switch") == "/切换"
    assert text("switch", "Alice") == "/切换 Alice"
    assert text("current") == "/当前"
    assert text("help") == "/帮助"
    assert text("bind") == "/绑定"
    assert text("bind", "CODE") == "/绑定 CODE"
    assert text("unknown") == "/帮助"


def test_default_client_factory_is_commands_bot_with_slash_commands():
    import discord
    from discord.ext import commands

    manager = DiscordStreamManager(db_engine=_engine())
    client = manager._default_client_factory("token", lambda message: None)
    assert isinstance(client, commands.Bot)
    assert isinstance(client, discord.Client)
    names = sorted(command.name for command in client.tree.get_commands())
    assert names == ["bind", "current", "employee", "help", "switch"]
    assert callable(getattr(client, "_sync_commands", None))
    # 注册事件后 on_message 仍由显式 handler 覆盖(与存量一致)
    assert callable(getattr(client, "on_message", None))


def test_default_client_factory_slash_commands_disabled_skips_command_tree():
    """§3.1 features.slash_commands=false:不注册命令树也不同步,文本处理保留。"""
    manager = DiscordStreamManager(db_engine=_engine())
    client = manager._default_client_factory(
        "token", lambda message: None, slash_commands=False
    )
    assert client.tree.get_commands() == []
    # on_ready 因缺少 _sync_commands 自动跳过命令同步
    assert not callable(getattr(client, "_sync_commands", None))
    assert callable(getattr(client, "on_message", None))


def test_normalize_command_message_passes_without_mention():
    inbound = normalize_discord_message(
        _raw(mentions=[], content="/切换 Alice", command="/switch")
    )
    assert inbound is not None
    assert inbound.text == "/切换 Alice"
    # 无 command 字段的群聊未 @bot 消息仍然拒绝
    assert normalize_discord_message(_raw(mentions=[])) is None


class _FakeClientWithSync:
    """最小 discord client:start 触发 on_ready,带可计数的 _sync_commands。"""

    def __init__(self):
        self._handlers = {}
        self.sync_calls = 0
        self._closed = threading.Event()

    def event(self, coro):
        self._handlers[coro.__name__] = coro
        return coro

    async def _sync_commands(self):
        self.sync_calls += 1

    async def start(self, token: str) -> None:
        if "on_ready" in self._handlers:
            await self._handlers["on_ready"]()
        await asyncio.to_thread(self._closed.wait)

    async def close(self) -> None:
        self._closed.set()


def test_gateway_on_ready_syncs_commands():
    import time

    db_engine = _engine()
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant-1", name="Tenant"))
        db.add(_binding(id="chan-1"))
        db.commit()
    clients = []

    def factory(token, on_message):
        client = _FakeClientWithSync()
        clients.append(client)
        return client

    manager = DiscordStreamManager(db_engine=db_engine, client_factory=factory)
    manager.ensure_binding("chan-1")
    deadline = time.time() + 8.0
    while time.time() < deadline and (not clients or not clients[0].sync_calls):
        time.sleep(0.02)
    assert clients and clients[0].sync_calls >= 1
    manager.stop_binding("chan-1")
    assert manager.wait_binding_stopped("chan-1", timeout_seconds=3.0)


# ---------------------------------------------------------------- 功能7 语音


def test_channel_capabilities_default_excludes_voice():
    adapter = DiscordAdapter(client_factory=lambda: _RoutingClient({}))
    assert adapter.channel_capabilities() == {
        ChannelCapability.SLASH_COMMANDS,
        ChannelCapability.THREADS,
        ChannelCapability.BACKFILL,
        ChannelCapability.TYPING,
        ChannelCapability.RICH_MEDIA,
    }


def test_channel_capabilities_voice_flag(monkeypatch):
    monkeypatch.setattr("app.channels.adapters.discord.shutil.which", lambda name: "/usr/bin/ffmpeg")
    adapter = DiscordAdapter(client_factory=lambda: _RoutingClient({}))
    assert ChannelCapability.VOICE not in adapter.channel_capabilities(_binding())
    assert ChannelCapability.VOICE in adapter.channel_capabilities(
        _binding(config_json={"bot_id": "bot-1", "features": {"voice": True}})
    )


def test_channel_capabilities_voice_requires_ffmpeg(monkeypatch):
    monkeypatch.setattr("app.channels.adapters.discord.shutil.which", lambda name: None)
    adapter = DiscordAdapter(client_factory=lambda: _RoutingClient({}))
    assert ChannelCapability.VOICE not in adapter.channel_capabilities(
        _binding(config_json={"bot_id": "bot-1", "features": {"voice": True}})
    )


def test_send_voice_missing_ffmpeg_is_permanent(monkeypatch, tmp_path):
    monkeypatch.setattr("app.channels.adapters.discord.shutil.which", lambda name: None)
    audio_file = tmp_path / "clip.mp3"
    audio_file.write_bytes(b"fake")
    adapter = DiscordAdapter(client_factory=lambda: _RoutingClient({}))
    with pytest.raises(DiscordPermanentError) as excinfo:
        adapter.send_voice(
            _binding(),
            {"voice_channel_id": "123"},
            {"type": "file", "file_ref": str(audio_file)},
        )
    assert "ffmpeg" in str(excinfo.value)


def test_send_voice_tts_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr("app.channels.adapters.discord.shutil.which", lambda name: "/usr/bin/ffmpeg")
    adapter = DiscordAdapter(client_factory=lambda: _RoutingClient({}))
    with pytest.raises(DiscordPermanentError) as excinfo:
        adapter.send_voice(_binding(), {"voice_channel_id": "123"}, {"type": "tts", "text": "hi"})
    assert "TTS" in str(excinfo.value)


def test_send_voice_missing_file_is_permanent(monkeypatch):
    monkeypatch.setattr("app.channels.adapters.discord.shutil.which", lambda name: "/usr/bin/ffmpeg")
    adapter = DiscordAdapter(client_factory=lambda: _RoutingClient({}))
    with pytest.raises(DiscordPermanentError) as excinfo:
        adapter.send_voice(
            _binding(),
            {"voice_channel_id": "123"},
            {"type": "file", "file_ref": "/nonexistent/clip.mp3"},
        )
    assert "不存在" in str(excinfo.value)


def test_send_voice_gateway_not_running(monkeypatch, tmp_path):
    monkeypatch.setattr("app.channels.adapters.discord.shutil.which", lambda name: "/usr/bin/ffmpeg")
    audio_file = tmp_path / "clip.mp3"
    audio_file.write_bytes(b"fake")
    adapter = DiscordAdapter(client_factory=lambda: _RoutingClient({}))
    import app.channels as channels_module

    monkeypatch.setattr(
        channels_module,
        "get_discord_stream_manager",
        lambda: SimpleNamespace(get_loop=lambda bid: None, get_client=lambda bid: None),
    )
    with pytest.raises(DiscordTransientError):
        adapter.send_voice(
            _binding(),
            {"voice_channel_id": "123"},
            {"type": "file", "file_ref": str(audio_file)},
        )


def test_play_voice_async_joins_plays_disconnects(tmp_path):
    class FakeVoiceClient:
        def __init__(self):
            self.playing = True
            self.played = False
            self.disconnected = False

        def play(self, source):
            self.played = True

        def is_playing(self):
            if self.played and self.playing:
                self.playing = False
                return True
            return False

        async def disconnect(self):
            self.disconnected = True

    class FakeChannel:
        def __init__(self, voice_client):
            self._voice_client = voice_client

        async def connect(self):
            return self._voice_client

    voice_client = FakeVoiceClient()
    channel = FakeChannel(voice_client)
    client = SimpleNamespace(get_channel=lambda cid: channel if cid == 123 else None)
    audio_file = tmp_path / "clip.mp3"
    audio_file.write_bytes(b"fake")
    adapter = DiscordAdapter(client_factory=lambda: _RoutingClient({}))
    asyncio.run(adapter._play_voice_async(client, 123, str(audio_file)))
    assert voice_client.played is True
    assert voice_client.disconnected is True
