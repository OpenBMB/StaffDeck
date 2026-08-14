from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from app.channels.adapters.base import (
    CHANNEL_TEXT_LIMIT,
    ChannelAdapter,
    ChannelInbound,
    register_channel_adapter,
    split_channel_text,
)
from app.channels.crypto import decrypt_channel_secret

if TYPE_CHECKING:
    from app.db.models import ChannelBinding

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_USERS_ME_API = f"{DISCORD_API_BASE}/users/@me"
DISCORD_MESSAGE_API = f"{DISCORD_API_BASE}/channels/{{channel_id}}/messages"

# Discord 机器人 mention 语法: <@123456789012345678> 或带昵称 <@!123456789012345678>
_DISCORD_MENTION_PATTERN = re.compile(r"^\s*<@!?\d+>\s*")


class DiscordSendError(RuntimeError):
    """Discord 发送失败的基类,默认可重试。"""

    retryable = True


class DiscordPermanentError(DiscordSendError):
    """凭证失效/权限不足等重试无意义的错误。"""

    retryable = False


class DiscordTransientError(DiscordSendError):
    """网络抖动/限流/服务端错误等可重试错误。"""

    retryable = True


def normalize_discord_message(raw: Any, *, account_scope: str = "") -> ChannelInbound | None:
    """把一条 Discord 消息归一化为 ChannelInbound。

    raw 由网关线程从 discord.py 的 Message 对象提取,字段:
      id / channel_id / guild_id / author_id / author_name / content /
      mentions(被 @ 的用户 id 列表) / bot_user_id(本机器人的用户 id) / is_group
    """
    if not isinstance(raw, dict):
        return None
    message_id = str(raw.get("id") or "").strip()
    channel_id = str(raw.get("channel_id") or "").strip()
    author_id = str(raw.get("author_id") or "").strip()
    bot_user_id = str(raw.get("bot_user_id") or "").strip()
    text = str(raw.get("content") or "").strip()
    if not message_id or not channel_id or not author_id:
        return None
    # 忽略机器人自己发的消息。
    if bot_user_id and author_id == bot_user_id:
        return None
    is_group = bool(raw.get("is_group"))
    mentions = [str(m) for m in (raw.get("mentions") or [])]
    # 群聊只响应明确 @bot 的消息;私聊不受此限制。
    if is_group and not (bot_user_id and bot_user_id in mentions):
        return None
    if not text:
        return None
    # 去掉消息开头的机器人 mention,保留其余内容。
    cleaned = _DISCORD_MENTION_PATTERN.sub("", text).strip()
    if not cleaned:
        return None
    if is_group:
        guild_id = str(raw.get("guild_id") or "").strip()
        session_id = channel_id
        group_id = guild_id or channel_id
    else:
        # 私聊以发送者为会话维度,便于跨 DM 频道稳定关联。
        session_id = f"dm:{author_id}"
        group_id = ""
    return ChannelInbound(
        channel="discord",
        event_id=message_id,
        from_user_id=author_id,
        to_user_id=bot_user_id,
        session_id=session_id,
        group_id=group_id,
        context_token="",
        text=cleaned,
        is_group=is_group,
        raw=raw,
        sender_name=str(raw.get("author_name") or "").strip(),
        account_scope=account_scope.strip(),
    )


def _credential(binding: ChannelBinding) -> tuple[str, str]:
    """返回 (bot_id, bot_token);缺凭证抛 DiscordPermanentError。"""
    config = dict(binding.config_json or {})
    bot_id = str(config.get("bot_id") or "").strip()
    token = decrypt_channel_secret(binding.credentials_enc) if binding.credentials_enc else ""
    if not bot_id or not token:
        raise DiscordPermanentError("Discord 绑定缺少应用凭证")
    return bot_id, token


def validate_discord_credentials(bot_token: str, *, client_factory: Callable[[], httpx.Client] | None = None) -> dict[str, str] | None:
    """调用 Discord 官方接口校验 Bot Token,返回 {bot_id, bot_name}。

    空 token 返回 None;凭证错误抛 DiscordPermanentError;网络问题抛 DiscordTransientError。
    """
    token = (bot_token or "").strip()
    if not token:
        return None
    client_factory = client_factory or (lambda: httpx.Client(timeout=15.0))
    try:
        with client_factory() as client:
            response = client.get(
                DISCORD_USERS_ME_API,
                headers={"Authorization": f"Bot {token}"},
            )
            if response.status_code in (401, 403):
                raise DiscordPermanentError("Discord Bot Token 无效或已被吊销")
            if response.status_code >= 500 or response.status_code == 429:
                raise DiscordTransientError(f"Discord 接口暂不可用 (HTTP {response.status_code})")
            if response.status_code >= 400:
                raise DiscordPermanentError(f"Discord 接口拒绝请求 (HTTP {response.status_code})")
            data = response.json()
            bot_id = str(data.get("id") or "").strip()
            bot_name = str(data.get("username") or "").strip()
            if not bot_id:
                raise DiscordPermanentError("Discord 接口未返回机器人标识")
            return {"bot_id": bot_id, "bot_name": bot_name or "Discord 机器人"}
    except DiscordSendError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise DiscordTransientError(str(exc)) from exc


class DiscordAdapter(ChannelAdapter):
    """Discord 渠道适配器:Gateway 长连接入站(线程模式) + REST 出站。"""

    def __init__(self, *, client_factory: Callable[[], httpx.Client] | None = None) -> None:
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=15.0))

    def normalize(self, raw: Any, *, account_scope: str = "") -> ChannelInbound | None:
        return normalize_discord_message(raw, account_scope=account_scope)

    def send(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        channel_id = str(target.get("channel_id") or "").strip()
        if not channel_id:
            raise DiscordPermanentError("Discord 目标缺少 channel_id")
        _bot_id, token = _credential(binding)
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        }
        try:
            with self._client_factory() as client:
                for chunk in split_channel_text(text, CHANNEL_TEXT_LIMIT):
                    response = client.post(
                        DISCORD_MESSAGE_API.format(channel_id=channel_id),
                        json={"content": chunk},
                        headers=headers,
                    )
                    if response.status_code in (401, 403):
                        raise DiscordPermanentError(
                            f"Discord 拒绝发送 (HTTP {response.status_code})"
                        )
                    if response.status_code >= 500 or response.status_code == 429:
                        raise DiscordTransientError(
                            f"Discord 接口暂不可用 (HTTP {response.status_code})"
                        )
                    if response.status_code >= 400:
                        raise DiscordPermanentError(
                            f"Discord 拒绝发送 (HTTP {response.status_code})"
                        )
        except DiscordSendError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DiscordTransientError(str(exc)) from exc

    def start_ingress(self, binding_id: str) -> None:
        from app.channels import get_discord_stream_manager

        get_discord_stream_manager().ensure_binding(binding_id)

    def stop_ingress(self, binding_id: str) -> None:
        from app.channels import get_discord_stream_manager

        get_discord_stream_manager().stop_binding(binding_id)


class DiscordStreamManager:
    """每 binding 一个 daemon 线程 + 线程内独立 asyncio loop 跑 discord.py 客户端。

    discord.py 2.x 的 loop 由 _async_setup_hook 从 asyncio.get_running_loop() 绑定,
    天然 per-instance,无需飞书那样的子进程隔离。
    """

    def __init__(
        self,
        *,
        db_engine=None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        from app.db import engine

        self._engine = db_engine or engine
        # client_factory(bot_token, on_message) -> 已挂载回调的 discord.Client 实例
        self._client_factory = client_factory
        self._threads: dict[str, threading.Thread] = {}
        self._stops: dict[str, threading.Event] = {}
        self._paused: set[str] = set()
        self._lock = threading.RLock()
        self._reconcile_stop = threading.Event()
        self._reconcile_thread: threading.Thread | None = None

    def ensure_binding(self, binding_id: str) -> None:
        with self._lock:
            if binding_id in self._paused:
                return
            thread = self._threads.get(binding_id)
            if thread is not None and thread.is_alive():
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self._run_binding,
                args=(binding_id, stop),
                name=f"staffdeck-discord-{binding_id}",
                daemon=True,
            )
            self._stops[binding_id] = stop
            self._threads[binding_id] = thread
            thread.start()

    def _run_binding(self, binding_id: str, stop: threading.Event) -> None:
        try:
            from app.channels.discord_runtime import DiscordEventHandler

            from app.db.models import ChannelBinding
            from sqlmodel import Session, select

            with Session(self._engine) as db:
                binding = db.exec(
                    select(ChannelBinding).where(ChannelBinding.id == binding_id)
                ).first()
                if binding is None or binding.channel != "discord" or binding.status != "active":
                    return
                bot_id, token = _credential(binding)
                expected_revision = binding.config_revision
            handler = DiscordEventHandler(
                db_engine=self._engine,
                binding_id=binding_id,
                expected_revision=expected_revision,
                bot_id=bot_id,
            )
            factory = self._client_factory or self._default_client_factory
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                loop.run_until_complete(
                    self._run_gateway(factory, token, handler, stop, binding_id)
                )
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
        except Exception:
            logger.exception("discord 绑定连接退出 binding=%s", binding_id)
        finally:
            with self._lock:
                self._threads.pop(binding_id, None)
                self._stops.pop(binding_id, None)

    def _default_client_factory(self, token: str, on_message):
        import discord

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def _on_message(message) -> None:
            await on_message(message)

        return client

    async def _run_gateway(self, factory, token: str, handler, stop: threading.Event, binding_id: str) -> None:
        import asyncio

        client = factory(token, handler.handle_message)

        async def mark_connected(connected: bool) -> None:
            await asyncio.to_thread(self._set_connected, binding_id, handler.expected_revision, connected)

        register_event = getattr(client, "event", None)
        if callable(register_event):
            async def _on_ready() -> None:
                await mark_connected(True)

            register_event(_on_ready)
        else:
            await mark_connected(True)

        try:
            start_task = asyncio.create_task(client.start(token))
            stop_task = asyncio.create_task(asyncio.to_thread(stop.wait))
            done, _ = await asyncio.wait(
                {start_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop.is_set():
                try:
                    await client.close()
                except Exception:
                    logger.exception("关闭 discord 客户端失败 binding=%s", binding_id)
            await start_task
        except Exception:
            logger.exception("discord 网关异常退出 binding=%s", binding_id)
        finally:
            try:
                await client.close()
            except Exception:
                logger.exception("关闭 discord 客户端失败 binding=%s", binding_id)
            await mark_connected(False)

    def _set_connected(self, binding_id: str, revision: int, connected: bool) -> None:
        try:
            from app.db.models import ChannelBinding
            from sqlmodel import Session, update

            with Session(self._engine) as db:
                db.exec(
                    update(ChannelBinding)
                    .where(
                        ChannelBinding.id == binding_id,
                        ChannelBinding.channel == "discord",
                        ChannelBinding.config_revision == revision,
                    )
                    .values(connected=connected)
                )
                db.commit()
        except Exception:
            logger.exception("更新 discord 连接状态失败 binding=%s", binding_id)

    def stop_binding(self, binding_id: str) -> None:
        stop = self._stops.get(binding_id)
        if stop is not None:
            stop.set()

    def pause_binding(self, binding_id: str) -> None:
        with self._lock:
            self._paused.add(binding_id)
        self.stop_binding(binding_id)

    def resume_binding(self, binding_id: str, *, start: bool = True) -> None:
        with self._lock:
            self._paused.discard(binding_id)
        if start:
            self.ensure_binding(binding_id)

    def wait_binding_stopped(self, binding_id: str, *, timeout_seconds: float = 5.0) -> bool:
        with self._lock:
            thread = self._threads.get(binding_id)
        if thread is None:
            return True
        thread.join(timeout=timeout_seconds)
        return not thread.is_alive()

    def _reconcile_loop(self) -> None:
        from app.db.models import ChannelBinding
        from sqlmodel import Session, select

        while not self._reconcile_stop.wait(5.0):
            try:
                with Session(self._engine) as db:
                    active = {
                        str(b.id)
                        for b in db.exec(
                            select(ChannelBinding).where(
                                ChannelBinding.channel == "discord",
                                ChannelBinding.status == "active",
                            )
                        ).all()
                    }
                with self._lock:
                    for binding_id in active:
                        if binding_id not in self._paused:
                            self.ensure_binding(binding_id)
                    stale = set(self._threads) - active
                for binding_id in stale:
                    self.stop_binding(binding_id)
            except Exception:
                logger.exception("discord reconcile 循环异常")

    def start(self) -> None:
        with self._lock:
            if self._reconcile_thread is not None and self._reconcile_thread.is_alive():
                return
            self._reconcile_stop.clear()
            self._reconcile_thread = threading.Thread(
                target=self._reconcile_loop,
                name="staffdeck-discord-reconcile",
                daemon=True,
            )
            self._reconcile_thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> bool:
        self._reconcile_stop.set()
        reconcile_thread = self._reconcile_thread
        if reconcile_thread is not None:
            reconcile_thread.join(timeout=timeout_seconds)
        with self._lock:
            binding_ids = list(self._threads)
        for binding_id in binding_ids:
            self.stop_binding(binding_id)
        stopped = True
        for binding_id in binding_ids:
            if not self.wait_binding_stopped(binding_id, timeout_seconds=timeout_seconds):
                stopped = False
        reconcile_alive = reconcile_thread is not None and reconcile_thread.is_alive()
        return stopped and not reconcile_alive


register_channel_adapter("discord", DiscordAdapter())
