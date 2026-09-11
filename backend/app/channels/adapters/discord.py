from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import httpx

from app.channels.adapters.base import (
    CHANNEL_TEXT_LIMIT,
    ChannelAdapter,
    ChannelCapability,
    ChannelInbound,
    ChannelInboundAttachment,
    register_channel_adapter,
    split_channel_text,
    stream_download_with_limit,
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

DISCORD_TYPING_API = f"{DISCORD_API_BASE}/channels/{{channel_id}}/typing"
DISCORD_THREADS_API = f"{DISCORD_API_BASE}/channels/{{channel_id}}/threads"

# 出站富媒体限制(与 Discord v10 一致):embeds≤10,各字段长度裁剪而非拒绝。
_MAX_EMBEDS = 10
_MAX_EMBED_TITLE = 256
_MAX_EMBED_DESCRIPTION = 4096
_MAX_EMBED_FIELDS = 25
_MAX_EMBED_FIELD_NAME = 256
_MAX_EMBED_FIELD_VALUE = 1024
_MAX_EMBED_FOOTER = 2048
# Discord 免费档单文件上限。
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
# fetch_history 的 Discord 单次上限。
_MAX_HISTORY_LIMIT = 100


def _features_slash_commands(binding: "ChannelBinding") -> bool:
    """§3.1 features.slash_commands 开关:缺失时默认开启,保持存量行为。"""
    features = (binding.config_json or {}).get("features") or {}
    return bool(features.get("slash_commands", True))


class DiscordSendError(RuntimeError):
    """Discord 发送失败的基类,默认可重试。"""

    retryable = True


class DiscordPermanentError(DiscordSendError):
    """凭证失效/权限不足等重试无意义的错误。"""

    retryable = False


class DiscordTransientError(DiscordSendError):
    """网络抖动/限流/服务端错误等可重试错误。"""

    retryable = True


def normalize_discord_message(
    raw: Any,
    *,
    account_scope: str = "",
    include_bot: bool = False,
    require_mention: bool = True,
) -> ChannelInbound | None:
    """把一条 Discord 消息归一化为 ChannelInbound。

    raw 由网关线程从 discord.py 的 Message 对象提取,字段:
      id / channel_id / guild_id / author_id / author_name / content /
      mentions(被 @ 的用户 id 列表) / bot_user_id(本机器人的用户 id) / is_group
    扩展字段(功能 2/5/8):
      is_thread / thread_id / parent_id(线程上下文,parent_id 用于白名单判定)
      attachments(每项 id/filename/content_type/size/url)
      command(原生斜杠命令名,命令消息不要求 @bot)
    include_bot=True 时不跳过机器人自身消息(回填用);require_mention=False 时
    群聊不要求 @bot(回填历史上下文用),均不影响入站默认行为。
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
    # 忽略机器人自己发的消息;回填需要看到 bot 说过的话,由 include_bot 覆盖。
    if not include_bot and bot_user_id and author_id == bot_user_id:
        return None
    is_group = bool(raw.get("is_group"))
    command = str(raw.get("command") or "").strip()
    mentions = [str(m) for m in (raw.get("mentions") or [])]
    # 群聊只响应明确 @bot 的消息;私聊与命令消息不受此限制。
    if is_group and require_mention and not command and not (bot_user_id and bot_user_id in mentions):
        return None
    if not text:
        return None
    # 去掉消息开头的机器人 mention,保留其余内容。
    cleaned = _DISCORD_MENTION_PATTERN.sub("", text).strip()
    if not cleaned:
        return None
    if is_group:
        guild_id = str(raw.get("guild_id") or "").strip()
        # 线程消息的会话锚点=线程自身 ID(Discord 线程 channel_id 天然独立)。
        session_id = str(raw.get("thread_id") or "").strip() or channel_id
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
        attachments=_extract_attachments(raw.get("attachments")),
    )


def _extract_attachments(items: Any) -> list[ChannelInboundAttachment]:
    """把 Discord 消息事件 attachments 数组转为 ChannelInboundAttachment 列表。"""
    result: list[ChannelInboundAttachment] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        media_id = str(item.get("id") or "").strip()
        url = str(item.get("url") or "").strip()
        if not media_id or not url:
            continue
        content_type = str(item.get("content_type") or "").strip()
        result.append(
            ChannelInboundAttachment(
                media_id=media_id,
                kind="image" if content_type.startswith("image/") else "file",
                filename=str(item.get("filename") or "").strip(),
                content_type=content_type,
                size=int(item.get("size") or 0),
                download_params={"url": url},
            )
        )
    return result


def _validate_embeds(embeds: Any) -> list[dict[str, Any]]:
    """裁剪 embeds 到 Discord 限制内,超限降级而非拒绝(功能8,§4.8 R8)。

    规则:数量≤10、title≤256、description≤4096、fields≤25、
    field name≤256/value≤1024、footer≤2048;未知字段丢弃。
    """
    if not isinstance(embeds, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in embeds[: _MAX_EMBEDS]:
        if not isinstance(item, dict):
            continue
        embed: dict[str, Any] = {}
        title = str(item.get("title") or "").strip()
        if title:
            embed["title"] = title[:_MAX_EMBED_TITLE]
        description = str(item.get("description") or "").strip()
        if description:
            embed["description"] = description[:_MAX_EMBED_DESCRIPTION]
        color = item.get("color")
        if color is not None:
            try:
                embed["color"] = int(color)
            except (TypeError, ValueError):
                logger.warning("discord embed color 非法,已丢弃: %r", color)
        url = str(item.get("url") or "").strip()
        if url:
            embed["url"] = url
        image = item.get("image")
        if isinstance(image, dict) and str(image.get("url") or "").strip():
            embed["image"] = {"url": str(image["url"]).strip()}
        footer = item.get("footer")
        if isinstance(footer, dict) and str(footer.get("text") or "").strip():
            embed["footer"] = {"text": str(footer["text"]).strip()[:_MAX_EMBED_FOOTER]}
        fields: list[dict[str, Any]] = []
        for field in (item.get("fields") or [])[: _MAX_EMBED_FIELDS]:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name") or "").strip()
            value = str(field.get("value") or "").strip()
            if not name or not value:
                continue
            entry: dict[str, Any] = {
                "name": name[:_MAX_EMBED_FIELD_NAME],
                "value": value[:_MAX_EMBED_FIELD_VALUE],
            }
            if bool(field.get("inline")):
                entry["inline"] = True
            fields.append(entry)
        if fields:
            embed["fields"] = fields
        if embed:
            cleaned.append(embed)
    return cleaned


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
        payload_json: str | None = None,
    ) -> None:
        """出站投递:纯文本分片发送;payload_json 携带 embeds/files 时走富媒体路径。

        payload_json 格式(功能8):{"content", "embeds": [...], "files": [
        {"filename", "data"(base64 或 bytes), "content_type"}]}。
        target.thread_id 优先于 channel_id(功能2:线程本质是独立频道端点)。
        """
        channel_id = str(target.get("thread_id") or target.get("channel_id") or "").strip()
        if not channel_id:
            raise DiscordPermanentError("Discord 目标缺少 channel_id")
        _bot_id, token = _credential(binding)
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        }
        rich = self._parse_rich_payload(payload_json)
        try:
            with self._client_factory() as client:
                if rich is None:
                    for index, chunk in enumerate(split_channel_text(text, CHANNEL_TEXT_LIMIT)):
                        payload: dict[str, Any] = {"content": chunk}
                        if idempotency_key:
                            payload["nonce"] = self._nonce(idempotency_key, index)
                        self._post_message(client, channel_id, payload, headers)
                    return
                body = dict(rich)
                content = str(rich.get("content") or "").strip() or text
                body["content"] = content[:CHANNEL_TEXT_LIMIT]
                if idempotency_key:
                    body["nonce"] = self._nonce(idempotency_key, 0)
                if body.get("files"):
                    self._post_message_multipart(client, channel_id, body, headers)
                else:
                    self._post_message(client, channel_id, body, headers)
        except DiscordSendError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DiscordTransientError(str(exc)) from exc

    @staticmethod
    def _nonce(idempotency_key: str, index: int) -> str:
        # Discord nonce 限 25 字符;按 idempotency_key+分片序号稳定派生,
        # 重试时同一分片得到相同 nonce,避免分片中断后重发产生重复消息
        digest = hashlib.sha256(f"{idempotency_key}:{index}".encode("utf-8")).hexdigest()
        return digest[:24]

    @staticmethod
    def _parse_rich_payload(payload_json: str | None) -> dict[str, Any] | None:
        """解析投递 payload_json;无富媒体字段时返回 None(走纯文本路径)。"""
        if not payload_json or not str(payload_json).strip():
            return None
        try:
            data = json.loads(payload_json)
        except (TypeError, ValueError) as exc:
            logger.warning("discord 投递 payload_json 解析失败,降级纯文本: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        embeds = _validate_embeds(data.get("embeds"))
        files = data.get("files") or []
        if not embeds and not files:
            return None
        return {"content": str(data.get("content") or "").strip(), "embeds": embeds, "files": files}

    @staticmethod
    def _prepare_files(files: Any) -> list[tuple[str, tuple[str, bytes, str]]]:
        """把 payload files 转为 httpx multipart 条目,单文件超 8MiB 抛永久错误。"""
        if not isinstance(files, list):
            raise DiscordPermanentError("discord payload files 必须是列表")
        entries: list[tuple[str, tuple[str, bytes, str]]] = []
        for index, item in enumerate(files):
            if not isinstance(item, dict):
                raise DiscordPermanentError("discord payload files 条目格式无效")
            filename = str(item.get("filename") or "").strip() or f"attachment-{index}"
            data = item.get("data")
            if isinstance(data, str):
                try:
                    data = base64.b64decode(data)
                except (ValueError, TypeError) as exc:
                    raise DiscordPermanentError(f"discord 附件 {filename} base64 解码失败") from exc
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise DiscordPermanentError(f"discord 附件 {filename} 缺少数据")
            if len(data) > _MAX_ATTACHMENT_BYTES:
                raise DiscordPermanentError(
                    f"discord 附件 {filename} 超过 {_MAX_ATTACHMENT_BYTES // (1024 * 1024)}MiB 上限"
                )
            content_type = str(item.get("content_type") or "").strip() or "application/octet-stream"
            entries.append((f"files[{index}]", (filename, bytes(data), content_type)))
        return entries

    def _post_message(
        self,
        client: httpx.Client,
        channel_id: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> None:
        response = client.post(
            DISCORD_MESSAGE_API.format(channel_id=channel_id),
            json=payload,
            headers=headers,
        )
        self._raise_for_status(response, "发送")

    def _post_message_multipart(
        self,
        client: httpx.Client,
        channel_id: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> None:
        files = self._prepare_files(body.get("files"))
        payload_field = {key: value for key, value in body.items() if key != "files"}
        response = client.post(
            DISCORD_MESSAGE_API.format(channel_id=channel_id),
            files=files,
            data={"payload_json": json.dumps(payload_field, ensure_ascii=False)},
            headers={"Authorization": headers["Authorization"]},
        )
        self._raise_for_status(response, "发送")

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        if response.status_code in (401, 403):
            raise DiscordPermanentError(f"Discord 拒绝{action} (HTTP {response.status_code})")
        if response.status_code >= 500 or response.status_code == 429:
            raise DiscordTransientError(f"Discord 接口暂不可用 (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise DiscordPermanentError(f"Discord 拒绝{action} (HTTP {response.status_code})")

    def send_typing(self, binding: ChannelBinding, target: dict[str, Any], status: int) -> None:
        """发送"正在输入"指示(status 1=开始 2=结束,语义对齐微信)。

        Discord typing 触发后约 10s 自动消失,无"停止"端点:status=2 直接返回。
        best-effort:任何失败仅记录日志,不阻塞主链路。
        """
        if int(status) != 1:
            return
        channel_id = str(target.get("thread_id") or target.get("channel_id") or "").strip()
        if not channel_id:
            return
        try:
            _bot_id, token = _credential(binding)
            with self._client_factory() as client:
                response = client.post(
                    DISCORD_TYPING_API.format(channel_id=channel_id),
                    headers={"Authorization": f"Bot {token}"},
                )
                self._raise_for_status(response, "发送 typing")
        except Exception:
            logger.warning(
                "discord typing 发送失败(忽略) binding=%s channel=%s",
                binding.id,
                channel_id,
                exc_info=True,
            )

    def create_thread(self, binding: ChannelBinding, target: dict[str, Any], name: str) -> str:
        """在 target 指定频道创建公开线程(GUILD_PUBLIC_THREAD),返回线程 ID。

        供 outbox 自动建线程链路调用;name 裁剪到 Discord 100 字符上限,
        错误分类复用 _raise_for_status(401/403/4xx 永久,429/5xx 可重试)。
        """
        channel_id = str(target.get("channel_id") or "").strip()
        if not channel_id:
            raise DiscordPermanentError("Discord 建线程目标缺少 channel_id")
        _bot_id, token = _credential(binding)
        headers = {"Authorization": f"Bot {token}"}
        try:
            with self._client_factory() as client:
                response = client.post(
                    DISCORD_THREADS_API.format(channel_id=channel_id),
                    json={"name": str(name)[:100], "type": 11},
                    headers=headers,
                )
                self._raise_for_status(response, "创建线程")
                thread_id = str(response.json().get("id") or "").strip()
        except DiscordSendError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DiscordTransientError(str(exc)) from exc
        if not thread_id:
            raise DiscordPermanentError("Discord 创建线程未返回线程 ID")
        return thread_id

    def fetch_history(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        *,
        before: str | None = None,
        after: str | None = None,
        limit: int = _MAX_HISTORY_LIMIT,
    ) -> list[ChannelInbound]:
        """拉取频道/线程历史消息并归一化(功能4);不跳过 bot 自身消息,不要求 @bot。"""
        channel_id = str(target.get("thread_id") or target.get("channel_id") or "").strip()
        if not channel_id:
            raise DiscordPermanentError("Discord 回填目标缺少 channel_id")
        limit = max(1, min(int(limit or 1), _MAX_HISTORY_LIMIT))
        _bot_id, token = _credential(binding)
        params: dict[str, Any] = {"limit": limit}
        if str(before or "").strip():
            params["before"] = str(before).strip()
        if str(after or "").strip():
            params["after"] = str(after).strip()
        try:
            with self._client_factory() as client:
                response = client.get(
                    DISCORD_MESSAGE_API.format(channel_id=channel_id),
                    params=params,
                    headers={"Authorization": f"Bot {token}"},
                )
                self._raise_for_status(response, "拉取历史")
                items = response.json()
        except DiscordSendError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DiscordTransientError(str(exc)) from exc
        if not isinstance(items, list):
            raise DiscordTransientError("Discord 历史消息响应格式无效")
        result: list[ChannelInbound] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = self._history_to_normalize_input(item)
            inbound = normalize_discord_message(
                normalized,
                include_bot=True,
                require_mention=False,
            )
            if inbound is not None:
                result.append(inbound)
        return result

    @staticmethod
    def _history_to_normalize_input(message: dict[str, Any]) -> dict[str, Any]:
        """把 Discord REST 历史消息响应映射为 normalize 的 dict 输入格式。"""
        channel = message.get("channel_id")
        guild = message.get("guild_id")
        author = message.get("author") or {}
        return {
            "id": str(message.get("id") or ""),
            "channel_id": str(channel or ""),
            "guild_id": str(guild or ""),
            "author_id": str(author.get("id") or ""),
            "author_name": str(author.get("global_name") or author.get("username") or ""),
            "content": str(message.get("content") or ""),
            "mentions": [str(user.get("id") or "") for user in (message.get("mentions") or [])],
            "bot_user_id": "",
            "is_group": bool(guild),
            "is_thread": False,
            # 历史消息原始时间戳(ISO 8601);web 回填合并按此排序
            "created_at": str(message.get("timestamp") or ""),
            "attachments": [
                {
                    "id": str(att.get("id") or ""),
                    "filename": str(att.get("filename") or ""),
                    "content_type": str(att.get("content_type") or ""),
                    "size": int(att.get("size") or 0),
                    "url": str(att.get("url") or ""),
                }
                for att in (message.get("attachments") or [])
                if isinstance(att, dict) and att.get("url")
            ],
        }

    def download_media(
        self,
        binding: ChannelBinding,
        attachment: ChannelInboundAttachment,
        *,
        max_bytes: int = 0,
    ) -> bytes:
        """下载入站附件字节(功能8);Discord CDN 公开无需鉴权头。"""
        url = str(attachment.download_params.get("url") or "").strip()
        if not url:
            raise DiscordPermanentError(f"discord 附件缺少下载地址 media_id={attachment.media_id}")
        try:
            with self._client_factory() as client:
                if max_bytes > 0:
                    status, data = stream_download_with_limit(
                        client, "GET", url, max_bytes=max_bytes
                    )
                    if status == 429 or status >= 500:
                        raise DiscordTransientError("Discord 附件下载服务暂时不可用")
                    if status >= 400:
                        raise DiscordPermanentError(f"Discord 拒绝附件下载 HTTP {status}")
                    return data
                response = client.get(url)
        except DiscordSendError:
            raise
        except ValueError:
            raise
        except (httpx.HTTPError, TypeError) as exc:
            raise DiscordTransientError("Discord 附件下载暂时失败") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise DiscordTransientError("Discord 附件下载服务暂时不可用")
        if response.status_code >= 400:
            raise DiscordPermanentError(f"Discord 拒绝附件下载 HTTP {response.status_code}")
        return response.content

    def channel_capabilities(
        self, binding: ChannelBinding | None = None
    ) -> set[ChannelCapability]:
        """能力声明(§3.2):voice 默认关闭,由 config_json.features.voice 与 ffmpeg 决定。"""
        capabilities = {
            ChannelCapability.SLASH_COMMANDS,
            ChannelCapability.THREADS,
            ChannelCapability.BACKFILL,
            ChannelCapability.TYPING,
            ChannelCapability.RICH_MEDIA,
        }
        if binding is not None:
            features = (binding.config_json or {}).get("features") or {}
            voice_enabled = bool(features.get("voice"))
            if voice_enabled and shutil.which("ffmpeg") is not None:
                capabilities.add(ChannelCapability.VOICE)
        return capabilities

    def send_voice(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        audio: dict[str, Any],
    ) -> None:
        """语音播报(功能7,最小闭环):加入语音频道播放音频文件。

        audio 结构:{"type": "tts"|"file", "text"|"file_ref"}。首版仅支持
        file 类型播放(TTS 合成未配置);ffmpeg 缺失抛永久错误明确提示。
        实际 join/play 必须投递到 gateway 线程的 asyncio loop 执行。
        """
        if shutil.which("ffmpeg") is None:
            raise DiscordPermanentError("服务器缺少 ffmpeg,无法播放语音")
        audio_type = str((audio or {}).get("type") or "").strip()
        if audio_type == "tts":
            raise DiscordPermanentError("语音 TTS 合成未配置,请改用 file 类型")
        file_ref = str((audio or {}).get("file_ref") or "").strip()
        if not file_ref:
            raise DiscordPermanentError("语音投递缺少音频文件(file_ref)")
        voice_channel_id = str(target.get("voice_channel_id") or "").strip()
        if not voice_channel_id:
            raise DiscordPermanentError("语音投递目标缺少 voice_channel_id")
        try:
            voice_channel_int = int(voice_channel_id)
        except ValueError as exc:
            raise DiscordPermanentError("语音频道 ID 无效") from exc
        if not os.path.isfile(file_ref):
            raise DiscordPermanentError(f"语音音频文件不存在: {file_ref}")
        from app.channels import get_discord_stream_manager

        manager = get_discord_stream_manager()
        loop = manager.get_loop(binding.id)
        client = manager.get_client(binding.id)
        if loop is None or client is None:
            raise DiscordTransientError("Discord 网关未连接,无法播放语音")
        import asyncio

        future = asyncio.run_coroutine_threadsafe(
            self._play_voice_async(client, voice_channel_int, file_ref),
            loop,
        )
        try:
            future.result(timeout=120)
        except DiscordSendError:
            raise
        except Exception as exc:
            raise DiscordPermanentError(f"语音播报失败: {exc}") from exc

    @staticmethod
    async def _play_voice_async(client: Any, voice_channel_id: int, file_path: str) -> None:
        import asyncio

        import discord

        channel = client.get_channel(voice_channel_id)
        if channel is None:
            raise DiscordPermanentError("找不到语音频道")
        voice_client = await channel.connect()
        try:
            voice_client.play(discord.FFmpegPCMAudio(file_path))
            while voice_client.is_playing():
                await asyncio.sleep(0.5)
        finally:
            try:
                await voice_client.disconnect()
            except Exception:
                logger.exception("断开 discord 语音连接失败")

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
        self._loops: dict[str, Any] = {}
        self._clients: dict[str, Any] = {}
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
            default_factory = self._default_client_factory
            factory = self._client_factory or default_factory
            import asyncio

            loop = asyncio.new_event_loop()
            self._loops[binding_id] = loop
            try:
                asyncio.set_event_loop(loop)
                loop.run_until_complete(
                    self._run_gateway(
                        factory,
                        token,
                        handler,
                        stop,
                        binding_id,
                        # 仅默认工厂支持开关参数;自定义工厂签名保持 (token, on_message)
                        slash_commands=(
                            _features_slash_commands(binding)
                            if factory is default_factory
                            else None
                        ),
                    )
                )
            finally:
                self._loops.pop(binding_id, None)
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

    def _default_client_factory(self, token: str, on_message, *, slash_commands: bool = True):
        import discord
        from discord.ext import commands

        intents = discord.Intents.default()
        intents.message_content = True
        # command_prefix 仅用于满足 commands.Bot 构造要求;on_message 事件仍由
        # 下方显式注册覆盖 internal handler,前缀命令解析不会执行。
        client = commands.Bot(command_prefix="", intents=intents, help_command=None)

        async def _on_message(message) -> None:
            await on_message(message)

        _on_message.__name__ = "on_message"
        client.event(_on_message)

        # §3.1 features.slash_commands 开关:关闭时只保留 on_message 文本处理,
        # 不注册命令树也不同步(on_ready 因缺少 _sync_commands 自动跳过)。
        if slash_commands:
            self._register_slash_commands(client, on_message)

            async def _sync_commands() -> None:
                await self._sync_slash_commands(client)

            # 供 _run_gateway 的 on_ready 在连接就绪后按 guild 同步命令树。
            setattr(client, "_sync_commands", _sync_commands)

        return client

    @staticmethod
    def _command_text(name: str, argument: str | None = None) -> str:
        """斜杠命令序列化为等价文本指令,复用 service_routing.parse_command 单一事实来源。"""
        argument = str(argument or "").strip()
        if name == "employee":
            return f"/切换 {argument}" if argument else "/员工"
        if name == "switch":
            return f"/切换 {argument}" if argument else "/切换"
        if name == "current":
            return "/当前"
        if name == "help":
            return "/帮助"
        if name == "bind":
            return f"/绑定 {argument}" if argument else "/绑定"
        return "/帮助"

    def _register_slash_commands(self, client, on_message) -> None:
        import discord

        async def _dispatch(
            interaction: discord.Interaction,
            command_name: str,
            argument: str | None = None,
        ) -> None:
            # 先立即确认交互(显示"正在思考"),处理结果经 durable inbox/outbox 回写。
            try:
                await interaction.response.defer()
            except Exception:
                logger.warning(
                    "discord 命令 defer 失败 command=%s", command_name, exc_info=True
                )
            user = getattr(interaction, "user", None)
            raw = {
                "id": str(getattr(interaction, "id", "") or ""),
                "channel_id": str(getattr(interaction, "channel_id", "") or ""),
                "guild_id": str(getattr(interaction, "guild_id", "") or ""),
                "author_id": str(getattr(user, "id", "") or ""),
                "author_name": str(
                    getattr(user, "display_name", "") or getattr(user, "name", "") or ""
                ),
                "content": self._command_text(command_name, argument),
                "mentions": [],
                "bot_user_id": str(getattr(client.user, "id", "") or ""),
                "is_group": bool(getattr(interaction, "guild_id", None)),
                "command": f"/{command_name}",
            }
            await on_message(raw)

        @client.tree.command(name="employee", description="查看或切换可调度员工")
        async def _employee(
            interaction: discord.Interaction, name: str | None = None
        ) -> None:
            await _dispatch(interaction, "employee", name)

        @client.tree.command(name="switch", description="切换到指定员工")
        async def _switch(
            interaction: discord.Interaction, name: str | None = None
        ) -> None:
            await _dispatch(interaction, "switch", name)

        @client.tree.command(name="current", description="查看当前员工")
        async def _current(interaction: discord.Interaction) -> None:
            await _dispatch(interaction, "current")

        @client.tree.command(name="help", description="显示可用指令")
        async def _help(interaction: discord.Interaction) -> None:
            await _dispatch(interaction, "help")

        @client.tree.command(name="bind", description="触发身份绑定码")
        async def _bind(interaction: discord.Interaction, code: str | None = None) -> None:
            await _dispatch(interaction, "bind", code)

    async def _sync_slash_commands(self, client) -> None:
        """按 guild 同步命令树;同步失败仅记日志,不影响连接状态(文本指令仍可用)。"""
        try:
            for guild in client.guilds:
                await client.tree.sync(guild=guild)
        except Exception:
            logger.warning("discord 斜杠命令同步失败(文本指令仍可用)", exc_info=True)

    def get_loop(self, binding_id: str):
        """返回 binding 网关线程的 asyncio loop;未运行返回 None。"""
        return self._loops.get(binding_id)

    def get_client(self, binding_id: str):
        """返回 binding 网关线程的 discord client;未运行返回 None。"""
        return self._clients.get(binding_id)

    async def _run_gateway(
        self,
        factory,
        token: str,
        handler,
        stop: threading.Event,
        binding_id: str,
        *,
        slash_commands: bool | None = None,
    ) -> None:
        import asyncio

        if slash_commands is None:
            # 外部注入的自定义 client_factory 签名固定为 (token, on_message),不转发开关
            client = factory(token, handler.handle_message)
        else:
            client = factory(token, handler.handle_message, slash_commands=slash_commands)
        self._clients[binding_id] = client

        async def mark_connected(connected: bool) -> None:
            await asyncio.to_thread(self._set_connected, binding_id, handler.expected_revision, connected)

        register_event = getattr(client, "event", None)
        if callable(register_event):
            async def _on_ready() -> None:
                await mark_connected(True)
                sync_commands = cast(Callable[[], Any], getattr(client, "_sync_commands", None))
                if callable(sync_commands):
                    try:
                        await sync_commands()
                    except Exception:
                        logger.exception("discord 命令同步异常 binding=%s", binding_id)

            _on_ready.__name__ = "on_ready"
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
            self._clients.pop(binding_id, None)
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

    def wait_binding_stopped(self, binding_id: str, timeout_seconds: float = 5.0) -> bool:
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
