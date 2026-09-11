from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx
from sqlalchemy import update
from sqlmodel import Session, select

from app.channels.adapters.base import (
    ChannelInbound,
    ChannelInboundAttachment,
    register_channel_adapter,
    split_channel_text,
    stream_download_with_limit,
)
from app.channels.crypto import decrypt_channel_secret
from app.channels.markdown_render import (
    ensure_code_fences,
    extract_dingtalk_title,
    has_markdown,
    split_markdown_by_lines,
)
from app.config import get_settings
from app.db import engine
from app.db.models import ChannelBinding

logger = logging.getLogger(__name__)

DINGTALK_API_BASE = "https://api.dingtalk.com/v1.0"
DINGTALK_OPEN_CONNECTION_API = f"{DINGTALK_API_BASE}/gateway/connections/open"
DINGTALK_ACCESS_TOKEN_API = f"{DINGTALK_API_BASE}/oauth2/accessToken"
DINGTALK_EMOTION_API = f"{DINGTALK_API_BASE}/robot/emotion"
DINGTALK_CARD_INSTANCE_API = f"{DINGTALK_API_BASE}/card/instances"
DINGTALK_CARD_CREATE_AND_DELIVER_API = f"{DINGTALK_API_BASE}/card/instances/createAndDeliver"
DINGTALK_CARD_STREAMING_API = f"{DINGTALK_API_BASE}/card/streaming"
DINGTALK_TEXT_LIMIT = 2000
DINGTALK_WEBHOOK_HOSTS = {"oapi.dingtalk.com", "api.dingtalk.com"}
TOKEN_REFRESH_SKEW_SECONDS = 300

# 卡片接口 403 时提示开通的权限名（实例写入 / 流式输出各不相同）。
DINGTALK_CARD_INSTANCE_PERMISSION = "『互动卡片实例写权限』(Card.Instance.Write)"
DINGTALK_CARD_STREAMING_PERMISSION = "『互动卡片流式输出权限』(Card.Streaming.Write)"

# 钉钉 trace 卡片默认模板：官方 dingtalk-stream SDK 内置的通用 AI Markdown
# 卡片（card_instance.AIMarkdownCardInstance 同款）。自带 flowStatus 状态条
# （1=处理中/2=输入中/3=执行完成/5=执行失败），msgTitle 为普通变量，
# msgContent 为流式内容槽——只有走 /card/streaming 接口其实时内容才会渲染，
# PUT /card/instances 的数据更新只改 flowStatus，内容要等定格后才可见。
# 若需自定义布局，可在 binding 的 config_json.card_template_id 覆盖。
DINGTALK_TRACE_CARD_TEMPLATE_ID = "382e4302-551d-4880-bf29-a30acfab2e71.schema"

# 钉钉未开放任意 emoji 的 reaction 接口，只提供固定的“思考中”表情流。
# 这三个常量取自钉钉机器人实践而非官方文档，真机联调需要复核其是否仍然有效。
DINGTALK_ACK_EMOTION_NAME = "🤔思考中"
DINGTALK_ACK_EMOTION_ID = "2659900"
DINGTALK_ACK_EMOTION_BACKGROUND_ID = "im_bg_1"
DINGTALK_ACK_EMOTION_TYPE = 2
# emotion/recall 与 emotion/reply 参数对称、不返回远端表情 ID，因此本地只需记录
# 一个“已挂上待撤回”的哨兵值，撤回时按同样参数重发即可。
DINGTALK_REACTION_HANDLE = f"emotion:{DINGTALK_ACK_EMOTION_ID}"
_TRANSIENT_API_CODES = {"system.err", "system.error"}


class DingTalkSendError(RuntimeError):
    retryable = True


class DingTalkPermanentError(DingTalkSendError):
    retryable = False


class DingTalkTransientError(DingTalkSendError):
    retryable = True


class DingTalkTokenProvider:
    """按绑定缓存 access token；sessionWebhook 出站不需要它，服务端 API 才需要。"""

    def __init__(self, *, client_factory: Callable[[], httpx.Client] | None = None):
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=10.0))
        self._cache: dict[tuple[str, str, int], tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[tuple[str, str, int], threading.Lock] = {}

    @staticmethod
    def _key(binding: ChannelBinding) -> tuple[str, str, int]:
        return (
            str(binding.external_account_key or ""),
            str(binding.provider_tenant_key or ""),
            binding.config_revision,
        )

    def invalidate(self, binding: ChannelBinding, *, expected_token: str | None = None) -> bool:
        with self._lock:
            key = self._key(binding)
            cached = self._cache.get(key)
            if expected_token is not None and cached and cached[0] != expected_token:
                return False
            self._cache.pop(key, None)
            return True

    def get(self, binding: ChannelBinding, *, force_refresh: bool = False) -> str:
        key = self._key(binding)
        with self._lock:
            cached = self._cache.get(key)
            if cached and not force_refresh and cached[1] > time.monotonic():
                return cached[0]
            observed_token = cached[0] if cached else None
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                cached = self._cache.get(key)
                # 并发刷新时只让第一个请求打远端，其余复用新 token。
                if cached and cached[1] > time.monotonic() and (
                    not force_refresh or cached[0] != observed_token
                ):
                    return cached[0]
            client_id, client_secret = _credential(binding)
            try:
                with self._client_factory() as client:
                    response = client.post(
                        DINGTALK_ACCESS_TOKEN_API,
                        json={"appKey": client_id, "appSecret": client_secret},
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise DingTalkTransientError("钉钉 token 请求暂时失败") from exc
            try:
                data = response.json()
            except ValueError as exc:
                raise DingTalkTransientError("钉钉 token 响应格式无效") from exc
            if response.status_code == 429 or response.status_code >= 500:
                raise DingTalkTransientError("钉钉 token 服务暂时不可用")
            if response.status_code >= 400:
                raise DingTalkPermanentError("钉钉应用凭证无效或无权限")
            token = str(data.get("accessToken") or "").strip()
            expires_in = int(data.get("expireIn") or 0)
            if not token or expires_in <= 0:
                raise DingTalkTransientError("钉钉 token 响应缺少必要字段")
            valid_for = max(1, expires_in - TOKEN_REFRESH_SKEW_SECONDS)
            with self._lock:
                self._cache[key] = (token, time.monotonic() + valid_for)
            return token


def _text_value(raw: dict[str, Any]) -> str:
    value = raw.get("text")
    if isinstance(value, dict):
        value = value.get("content")
    return str(value or "").strip()


def _richtext_text(raw: dict[str, Any]) -> str:
    """从 richtext 消息的 content.richText 数组中拼接文本。"""
    content = raw.get("content") or {}
    if not isinstance(content, dict):
        return ""
    rich_text = content.get("richText") or content.get("rich_text") or []
    if not isinstance(rich_text, list):
        return ""
    parts: list[str] = []
    for item in rich_text:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip().lower() == "text":
            parts.append(str(item.get("text") or ""))
    return "".join(parts).strip()


def _strip_bot_mention(text: str, raw: dict[str, Any], *, is_group: bool) -> str:
    """DingTalk may include the @ token in text.content as well as atUsers."""
    if not is_group or raw.get("isInAtList") is not True:
        return text
    # Stream payloads do not consistently include the display name in atUsers;
    # the first @ token is the bot mention for a message accepted by isInAtList.
    return re.sub(r"^\s*@[^\s]+\s*", "", text, count=1).strip()


def _extract_dingtalk_attachments(
    raw: dict[str, Any],
    msgtype: str,
) -> list[ChannelInboundAttachment]:
    """从钉钉消息提取图片/文件附件。

    钉钉 stream SDK 把图片/文件正文放在 raw.content 下:
    - picture: raw.content.downloadCode + raw.content.pictureDownloadCode
    - file: raw.content.downloadCode + raw.content.fileName
    - richtext: raw.content.richText 数组,遍历 type=="picture" 的条目取 downloadCode
    robotCode 在 download_media 时由适配器从 binding 配置解析(= client_id)。
    """
    attachments: list[ChannelInboundAttachment] = []
    content = raw.get("content") or {}
    if not isinstance(content, dict):
        content = {}
    if msgtype == "picture":
        download_code = str(content.get("downloadCode") or "").strip()
        if download_code:
            attachments.append(
                ChannelInboundAttachment(
                    media_id=download_code,
                    kind="image",
                    filename=download_code[:12],
                    content_type="",
                    download_params={"download_code": download_code, "type": "picture"},
                )
            )
    elif msgtype == "file":
        download_code = str(content.get("downloadCode") or "").strip()
        file_name = str(content.get("fileName") or "").strip()
        if download_code:
            attachments.append(
                ChannelInboundAttachment(
                    media_id=download_code,
                    kind="file",
                    filename=file_name or download_code[:12],
                    content_type="",
                    download_params={"download_code": download_code, "type": "file"},
                )
            )
    elif msgtype in {"richtext", "rich_text"}:
        rich_text = content.get("richText") or content.get("rich_text") or []
        if isinstance(rich_text, list):
            for item in rich_text:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip().lower() != "picture":
                    continue
                download_code = str(item.get("downloadCode") or "").strip()
                if download_code:
                    attachments.append(
                        ChannelInboundAttachment(
                            media_id=download_code,
                            kind="image",
                            filename=download_code[:12],
                            content_type="",
                            download_params={"download_code": download_code, "type": "picture"},
                        )
                    )
    return attachments


def normalize_dingtalk_message(raw: dict[str, Any], *, account_scope: str = "") -> ChannelInbound | None:
    """Normalize a DingTalk Stream chatbot callback payload."""
    if not isinstance(raw, dict):
        return None
    msgtype = str(raw.get("msgtype") or "").strip().lower()
    # 放宽 msgtype 过滤:允许 text / picture / file / richtext
    if msgtype not in {"text", "picture", "file", "richtext", "rich_text"}:
        return None
    sender_id = str(raw.get("senderStaffId") or "").strip()
    if not sender_id:
        return None
    if sender_id == str(raw.get("chatbotUserId") or "").strip():
        return None
    message_id = str(raw.get("msgId") or "").strip()
    conversation_id = str(raw.get("conversationId") or "").strip()
    if not message_id or not conversation_id:
        return None

    # 提取图片/文件附件(picture/file 消息)
    attachments = _extract_dingtalk_attachments(raw, msgtype)

    # 文本在 text 类型时从 raw.text 提取,richtext 类型时从 content.richText 数组提取
    text = ""
    if msgtype == "text":
        text = _text_value(raw)
    elif msgtype in {"richtext", "rich_text"}:
        text = _richtext_text(raw)
    if not text and not attachments:
        return None

    is_group = str(raw.get("conversationType") or "") == "2"
    if is_group and raw.get("isInAtList") is not True:
        return None
    if text:
        text = _strip_bot_mention(text, raw, is_group=is_group)
        if not text and not attachments:
            return None
    context_token = str(raw.get("sessionWebhook") or "").strip()
    if not context_token:
        return None
    return ChannelInbound(
        channel="dingtalk",
        event_id=message_id,
        from_user_id=sender_id,
        to_user_id=str(raw.get("chatbotUserId") or "").strip(),
        session_id=conversation_id,
        group_id=conversation_id if is_group else "",
        context_token=context_token,
        text=text,
        is_group=is_group,
        raw=raw,
        sender_name=str(raw.get("senderNick") or "").strip(),
        account_scope=account_scope.strip(),
        attachments=attachments,
    )


def validate_dingtalk_credentials(
    client_id: str,
    client_secret: str,
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, str]:
    """Validate credentials by opening a Stream connection subscription."""
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id or not client_secret:
        raise DingTalkPermanentError("钉钉 Client ID 与 Client Secret 均不能为空")
    factory = client_factory or (lambda: httpx.Client(timeout=10.0))
    body = {
        "clientId": client_id,
        "clientSecret": client_secret,
        "subscriptions": [{"type": "CALLBACK", "topic": "/v1.0/im/bot/messages/get"}],
        "ua": "staffdeck",
    }
    try:
        with factory() as client:
            response = client.post(DINGTALK_OPEN_CONNECTION_API, json=body)
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DingTalkTransientError("无法验证钉钉应用凭证") from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise DingTalkTransientError("钉钉服务暂时不可用")
    if response.status_code >= 400:
        raise DingTalkPermanentError("钉钉应用凭证无效或无权限")
    if not data.get("endpoint") or not data.get("ticket"):
        raise DingTalkPermanentError("钉钉凭证未开通机器人 Stream 能力")
    return {"bot_name": "钉钉机器人"}


def _credential(binding: ChannelBinding) -> tuple[str, str]:
    config = dict(binding.config_json or {})
    client_id = str(config.get("client_id") or "").strip()
    secret = decrypt_channel_secret(binding.credentials_enc or "") if binding.credentials_enc else ""
    if not client_id or not secret:
        raise DingTalkPermanentError("钉钉绑定缺少应用凭证")
    return client_id, secret


def validate_dingtalk_webhook(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in DINGTALK_WEBHOOK_HOSTS


def _emotion_body(
    robot_code: str,
    message_id: str,
    conversation_id: str,
    emotion_name: str,
) -> dict[str, Any]:
    return {
        "robotCode": robot_code,
        "openMsgId": message_id,
        "openConversationId": conversation_id,
        "emotionType": DINGTALK_ACK_EMOTION_TYPE,
        "emotionName": emotion_name,
        "textEmotion": {
            "emotionId": DINGTALK_ACK_EMOTION_ID,
            "emotionName": emotion_name,
            "text": emotion_name,
            "backgroundId": DINGTALK_ACK_EMOTION_BACKGROUND_ID,
        },
    }


def _error_detail(response) -> tuple[str, str]:
    """提取钉钉错误响应中的 code/message（无 JSON 体时返回空串）。"""
    try:
        data = response.json() or {}
    except ValueError:
        return "", ""
    return (
        str(data.get("code") or "").strip(),
        str(data.get("message") or data.get("msg") or "").strip(),
    )


def _dingtalk_card_delivery(binding: ChannelBinding, target: dict[str, Any]) -> dict[str, Any]:
    """从投递目标解析卡片投放参数（openSpaceId + 群/机器人投放模型）。

    群聊（conversationType=="2"）投放到 dtv1.card//IM_GROUP.{openConversationId}，
    单聊投放到 dtv1.card//IM_ROBOT.{senderStaffId}，与官方 dingtalk-stream SDK
    CardReplier 的投放规则一致。robotCode 即绑定的 client_id。
    """
    robot_code, _ = _credential(binding)
    conversation_type = str(target.get("conversation_type") or "").strip()
    if conversation_type == "2":
        conversation_id = str(target.get("conversation_id") or "").strip() or str(
            target.get("to_user_id") or ""
        ).strip()
        if not conversation_id:
            raise DingTalkPermanentError("钉钉卡片投放缺少群会话标识")
        return {
            "openSpaceId": f"dtv1.card//IM_GROUP.{conversation_id}",
            "imGroupOpenDeliverModel": {"robotCode": robot_code},
        }
    staff_id = str(target.get("to_user_id") or "").strip()
    if not staff_id:
        raise DingTalkPermanentError("钉钉卡片投放缺少会话用户标识")
    return {
        "openSpaceId": f"dtv1.card//IM_ROBOT.{staff_id}",
        "imRobotOpenDeliverModel": {"spaceType": "IM_ROBOT"},
    }


class DingTalkAdapter:
    # 钉钉不提供“查询我加过的表情”接口，无法在崩溃后回查远端状态；但 emotion/reply
    # 参数固定且可重复提交，因此重试路径直接重发而不是回查。
    reaction_attach_idempotent = True
    reaction_token = DINGTALK_ACK_EMOTION_NAME

    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
        token_provider: DingTalkTokenProvider | None = None,
    ):
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=15.0))
        self._tokens = token_provider or DingTalkTokenProvider(client_factory=self._client_factory)

    def normalize(self, raw: dict[str, Any]) -> ChannelInbound | None:
        return normalize_dingtalk_message(raw)

    def _emotion_request(
        self,
        binding: ChannelBinding,
        endpoint: str,
        target: dict[str, Any],
        emotion_name: str,
    ) -> None:
        # robotCode 取绑定配置里的 client_id，不从可变入站 payload 里推断。
        robot_code, _ = _credential(binding)
        message_id = str(target.get("message_id") or "").strip()
        conversation_id = str(target.get("conversation_id") or "").strip()
        emotion_name = str(emotion_name or "").strip() or DINGTALK_ACK_EMOTION_NAME
        if not message_id or not conversation_id:
            raise DingTalkPermanentError("钉钉表情回写缺少消息或会话标识")
        body = _emotion_body(robot_code, message_id, conversation_id, emotion_name)
        url = f"{DINGTALK_EMOTION_API}/{endpoint}"
        force_refresh = False
        for attempt in range(2):
            token = self._tokens.get(binding, force_refresh=force_refresh)
            force_refresh = False
            try:
                with self._client_factory() as client:
                    response = client.post(
                        url,
                        json=body,
                        headers={
                            "x-acs-dingtalk-access-token": token,
                            "Content-Type": "application/json",
                        },
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise DingTalkTransientError("钉钉表情回写暂时失败") from exc
            if response.status_code == 401 and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise DingTalkTransientError("钉钉表情服务暂时不可用")
            if response.status_code == 403:
                raise DingTalkPermanentError("钉钉应用缺少机器人表情回写权限")
            if response.status_code >= 400:
                try:
                    code = str((response.json() or {}).get("code") or "").strip().lower()
                except ValueError:
                    code = ""
                if code in _TRANSIENT_API_CODES:
                    raise DingTalkTransientError("钉钉表情服务暂时不可用")
                raise DingTalkPermanentError(
                    f"钉钉拒绝表情回写 HTTP {response.status_code} code={code or '-'}"
                )
            return
        raise DingTalkPermanentError("钉钉 token 刷新后仍无法回写表情")

    def add_reaction(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        emotion_name: str = DINGTALK_ACK_EMOTION_NAME,
    ) -> str:
        self._emotion_request(binding, "reply", target, emotion_name)
        return DINGTALK_REACTION_HANDLE

    def remove_reaction(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        handle: str = "",
    ) -> None:
        # handle 只是本地哨兵，撤回按与 reply 相同的参数提交，重复调用无副作用。
        self._emotion_request(
            binding,
            "recall",
            target,
            str(target.get("reaction_token") or "") or DINGTALK_ACK_EMOTION_NAME,
        )

    def download_media(
        self,
        binding: ChannelBinding,
        attachment: ChannelInboundAttachment,
        *,
        max_bytes: int = 0,
    ) -> bytes:
        """钉钉附件下载:先获取下载 URL,再下载文件内容。

        第一步: POST /robot/messageFiles/download
            body = {"downloadCode": "...", "robotCode": "<client_id>"}
            返回 {"downloadUrl": "..."}
        第二步: GET downloadUrl, 超时 15s。
        robotCode 取 binding 配置的 client_id(_credential 第一个返回值)。
        复用 DingTalkTokenProvider 的 token 刷新重试模式(401 -> invalidate -> 重试一次)。
        max_bytes > 0 时第二步流式读取,超过上限立即中止。
        """
        download_code = str(
            attachment.download_params.get("download_code") or attachment.media_id
        )
        if not download_code:
            raise DingTalkPermanentError("钉钉附件下载缺少 downloadCode")
        robot_code, _ = _credential(binding)  # robotCode = client_id

        # 第一步:获取下载 URL
        url = f"{DINGTALK_API_BASE}/robot/messageFiles/download"
        force_refresh = False
        download_url: str = ""
        for attempt in range(2):
            token = self._tokens.get(binding, force_refresh=force_refresh)
            force_refresh = False
            try:
                with self._client_factory() as client:
                    response = client.post(
                        url,
                        json={"downloadCode": download_code, "robotCode": robot_code},
                        headers={
                            "x-acs-dingtalk-access-token": token,
                            "Content-Type": "application/json",
                        },
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise DingTalkTransientError("钉钉附件下载暂时失败") from exc
            if response.status_code == 401 and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise DingTalkTransientError("钉钉附件下载服务暂时不可用")
            if response.status_code >= 400:
                raise DingTalkPermanentError(f"钉钉拒绝附件下载 HTTP {response.status_code}")
            try:
                data = response.json()
            except ValueError as exc:
                raise DingTalkTransientError("钉钉附件下载响应格式无效") from exc
            download_url = str(data.get("downloadUrl") or "").strip()
            if not download_url:
                raise DingTalkPermanentError("钉钉附件下载响应缺少 downloadUrl")
            break
        else:
            raise DingTalkPermanentError("钉钉 token 刷新后仍无法下载附件")

        # 第二步:从 downloadUrl 下载实际文件
        # downloadUrl 自带签名 query string,不传 params 避免 httpx 重新编码 URL 导致签名失效
        try:
            with self._client_factory() as client:
                if max_bytes > 0:
                    status, data = stream_download_with_limit(
                        client, "GET", download_url,
                        max_bytes=max_bytes,
                    )
                    if status != 200:
                        raise DingTalkTransientError(f"钉钉附件下载失败 HTTP {status}")
                    return data
                response = client.get(download_url, timeout=15.0)
        except ValueError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise DingTalkTransientError("钉钉附件下载暂时失败") from exc
        if response.status_code != 200:
            raise DingTalkTransientError(f"钉钉附件下载失败 HTTP {response.status_code}")
        return response.content

    def _card_request(
        self,
        binding: ChannelBinding,
        method: str,
        url: str,
        body: dict[str, Any],
        *,
        permission_hint: str = DINGTALK_CARD_INSTANCE_PERMISSION,
    ) -> None:
        """卡片 OpenAPI 请求：token 鉴权 + 401 刷新重试 + 错误分类。"""
        force_refresh = False
        for attempt in range(2):
            token = self._tokens.get(binding, force_refresh=force_refresh)
            force_refresh = False
            try:
                with self._client_factory() as client:
                    headers = {
                        "x-acs-dingtalk-access-token": token,
                        "Content-Type": "application/json",
                    }
                    if method == "PUT":
                        response = client.put(url, json=body, headers=headers)
                    else:
                        response = client.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise DingTalkTransientError("钉钉卡片请求暂时失败") from exc
            if response.status_code == 401 and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise DingTalkTransientError("钉钉卡片服务暂时不可用")
            if response.status_code >= 400:
                code, message = _error_detail(response)
                if code in _TRANSIENT_API_CODES:
                    raise DingTalkTransientError("钉钉卡片服务暂时不可用")
                if response.status_code == 403:
                    # 403 = 应用未开通对应卡片权限：到钉钉开放平台「权限管理」
                    # 申请后即恢复，每轮对话都会重试，无需重启服务。
                    raise DingTalkPermanentError(
                        f"钉钉应用缺少卡片权限(HTTP 403 code={code or '-'} {message})，"
                        f"请到钉钉开放平台为应用开通 {permission_hint}"
                    )
                raise DingTalkPermanentError(
                    f"钉钉拒绝卡片请求 HTTP {response.status_code} code={code or '-'} {message}"
                )
            return
        raise DingTalkPermanentError("钉钉 token 刷新后仍无法操作卡片")

    def create_card(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        card_data: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> str:
        """创建并投放一张卡片实例（POST /card/instances/createAndDeliver）。

        卡片实例标识 outTrackId 由 idempotency_key sha256 派生（64 位十六
        进制），重试复用同一标识；返回该标识供后续 update_card 使用。
        模板默认取官方通用 AI Markdown 卡片，binding 的
        config_json.card_template_id 可覆盖。
        """
        key = str(idempotency_key or "").strip()
        if not key:
            raise DingTalkPermanentError("钉钉卡片创建缺少幂等键")
        if not isinstance(card_data, dict) or not card_data:
            raise DingTalkPermanentError("钉钉卡片创建缺少卡片数据")
        config = binding.config_json if isinstance(binding.config_json, dict) else {}
        template_id = (
            str(config.get("card_template_id") or "").strip() or DINGTALK_TRACE_CARD_TEMPLATE_ID
        )
        out_track_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
        body: dict[str, Any] = {
            "cardTemplateId": template_id,
            "outTrackId": out_track_id,
            "cardData": {"cardParamMap": card_data},
            # 与官方 SDK AI 卡片路径一致走 STREAM 回调模式；卡片无按钮，
            # 未订阅的回调主题仅会被 SDK 记 warning，不影响投递。
            "callbackType": "STREAM",
            "imGroupOpenSpaceModel": {"supportForward": False},
            "imRobotOpenSpaceModel": {"supportForward": False},
            **_dingtalk_card_delivery(binding, target),
        }
        self._card_request(binding, "POST", DINGTALK_CARD_CREATE_AND_DELIVER_API, body)
        return out_track_id

    def update_card(
        self,
        binding: ChannelBinding,
        card_id: str,
        card_data: dict[str, Any],
    ) -> None:
        """全量更新卡片实例数据（PUT /card/instances，语义同飞书卡片 PATCH）。"""
        out_track_id = str(card_id or "").strip()
        if not out_track_id:
            raise DingTalkPermanentError("钉钉卡片更新缺少实例标识")
        if not isinstance(card_data, dict) or not card_data:
            raise DingTalkPermanentError("钉钉卡片更新缺少卡片数据")
        self._card_request(
            binding,
            "PUT",
            DINGTALK_CARD_INSTANCE_API,
            {"outTrackId": out_track_id, "cardData": {"cardParamMap": card_data}},
        )

    def stream_card(
        self,
        binding: ChannelBinding,
        card_id: str,
        key: str,
        content: str,
        *,
        finalize: bool = False,
        failed: bool = False,
    ) -> None:
        """流式更新卡片内容槽位（PUT /v1.0/card/streaming）。

        AI 卡片的流式内容槽（如 msgContent）只有走本接口内容才会实时渲染：
        PUT /card/instances 的数据更新只影响 flowStatus 等普通变量，流式槽
        内容要等 isFinalize 定格后才可见。content 始终全量发送（isFull=true），
        guid 每次唯一以驱动一次渲染；finalize=true 关闭流式通道并定格最终
        内容（随后通常跟一次 update_card 切换 flowStatus 完成态/失败态）。
        """
        out_track_id = str(card_id or "").strip()
        slot = str(key or "").strip()
        if not out_track_id or not slot:
            raise DingTalkPermanentError("钉钉卡片流式更新缺少实例标识或内容槽位")
        body = {
            "outTrackId": out_track_id,
            "guid": uuid.uuid1().hex,
            "key": slot,
            "content": str(content or ""),
            "isFull": True,
            "isFinalize": bool(finalize),
            "isError": bool(failed),
        }
        self._card_request(
            binding,
            "PUT",
            DINGTALK_CARD_STREAMING_API,
            body,
            permission_hint=DINGTALK_CARD_STREAMING_PERMISSION,
        )

    def send(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        webhook = str(target.get("session_webhook") or target.get("context_token") or "").strip()
        if not webhook or not validate_dingtalk_webhook(webhook) or not text.strip():
            raise DingTalkPermanentError("钉钉投递目标或文本无效")
        expires_ms = int(target.get("session_webhook_expired_time") or 0)
        if expires_ms and expires_ms <= int(datetime.now(tz=UTC).timestamp() * 1000):
            raise DingTalkPermanentError("钉钉会话回复地址已过期")
        rich_enabled = bool(get_settings().channel_rich_render_enabled)
        use_rich = rich_enabled and has_markdown(text)
        if use_rich:
            chunks = split_markdown_by_lines(text, DINGTALK_TEXT_LIMIT)
            if not chunks:
                chunks = [text]
        else:
            chunks = split_channel_text(text, DINGTALK_TEXT_LIMIT)
        try:
            with self._client_factory() as client:
                for chunk in chunks:
                    if use_rich:
                        fenced = ensure_code_fences(chunk)
                        body = {
                            "msgtype": "markdown",
                            "markdown": {
                                "title": extract_dingtalk_title(fenced),
                                "text": fenced,
                            },
                        }
                    else:
                        body = {"msgtype": "text", "text": {"content": chunk}}
                    response = client.post(
                        webhook,
                        json=body,
                        headers={"Content-Type": "application/json"},
                    )
                    data = response.json()
                    if response.status_code == 429 or response.status_code >= 500:
                        raise DingTalkTransientError("钉钉消息发送暂时失败")
                    if response.status_code >= 400 or int(data.get("errcode", 0) or 0) != 0:
                        raise DingTalkPermanentError("钉钉消息发送失败")
        except DingTalkSendError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise DingTalkTransientError("钉钉消息发送暂时失败") from exc

    def start_ingress(self, binding_id: str) -> None:
        from app.channels import get_dingtalk_stream_manager

        get_dingtalk_stream_manager().ensure_binding(binding_id)

    def stop_ingress(self, binding_id: str) -> None:
        from app.channels import get_dingtalk_stream_manager

        get_dingtalk_stream_manager().stop_binding(binding_id)


class DingTalkStreamManager:
    """One SDK client per active binding, with durable staging in the callback."""

    def __init__(self, *, db_engine=None, client_factory=None):
        self._engine = db_engine or engine
        self._client_factory = client_factory or self._default_client_factory
        self._threads: dict[str, threading.Thread] = {}
        self._stops: dict[str, threading.Event] = {}
        self._paused: set[str] = set()
        self._lock = threading.Lock()
        self._reconcile_stop = threading.Event()
        self._reconcile_thread: threading.Thread | None = None

    @staticmethod
    def _default_client_factory(client_id: str, secret: str, handler):
        from dingtalk_stream import Credential, DingTalkStreamClient

        client = DingTalkStreamClient(Credential(client_id, secret))
        client.register_callback_handler("/v1.0/im/bot/messages/get", handler)
        return client

    def ensure_binding(self, binding_id: str) -> None:
        with self._lock:
            if binding_id in self._paused:
                return
            if binding_id in self._threads and self._threads[binding_id].is_alive():
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self._run_binding,
                args=(binding_id, stop),
                name=f"staffdeck-dingtalk-{binding_id}",
                daemon=True,
            )
            self._stops[binding_id] = stop
            self._threads[binding_id] = thread
            thread.start()

    def _run_binding(self, binding_id: str, stop: threading.Event) -> None:
        from app.channels.dingtalk_runtime import DingTalkCallbackHandler

        try:
            with Session(self._engine) as db:
                binding = db.get(ChannelBinding, binding_id)
                if not binding or binding.status != "active" or binding.channel != "dingtalk":
                    return
                client_id, secret = _credential(binding)
                revision = binding.config_revision
            handler = DingTalkCallbackHandler(
                db_engine=self._engine,
                binding_id=binding_id,
                expected_revision=revision,
                client_id=client_id,
            )
            client = self._client_factory(client_id, secret, handler)
            asyncio.run(self._run_client(client, stop, lambda value: self._set_connected(binding_id, revision, value)))
        except Exception:
            logger.exception("钉钉绑定连接退出 binding=%s", binding_id)
        finally:
            with self._lock:
                self._threads.pop(binding_id, None)
                self._stops.pop(binding_id, None)

    @staticmethod
    async def _run_client(client, stop: threading.Event, on_connected: Callable[[bool], None]) -> None:
        """Run the SDK protocol with a manager-owned, stop-aware receive loop."""
        import websockets

        client.pre_start()
        while not stop.is_set():
            connection = await asyncio.to_thread(client.open_connection)
            if not connection:
                await asyncio.sleep(1)
                continue
            uri = f"{connection['endpoint']}?ticket={quote_plus(connection['ticket'])}"
            try:
                async with websockets.connect(uri) as websocket:
                    client.websocket = websocket
                    keepalive_task = asyncio.create_task(client.keepalive(websocket))
                    await asyncio.to_thread(on_connected, True)
                    try:
                        while not stop.is_set():
                            try:
                                raw_message = await asyncio.wait_for(websocket.recv(), timeout=0.5)
                            except asyncio.TimeoutError:
                                continue
                            result = await client.route_message(json.loads(raw_message))
                            if result == "disconnect":
                                return
                    finally:
                        keepalive_task.cancel()
                        await asyncio.gather(keepalive_task, return_exceptions=True)
            except Exception:
                if not stop.is_set():
                    logger.exception("钉钉 Stream 连接断开，准备重连")
                    await asyncio.sleep(1)
            finally:
                await asyncio.to_thread(on_connected, False)

    def _set_connected(self, binding_id: str, revision: int, connected: bool) -> None:
        with Session(self._engine) as db:
            db.exec(
                update(ChannelBinding)
                .where(
                    ChannelBinding.id == binding_id,
                    ChannelBinding.channel == "dingtalk",
                    ChannelBinding.config_revision == revision,
                )
                .values(connected=connected)
            )
            db.commit()

    def stop_binding(self, binding_id: str) -> None:
        with self._lock:
            stop = self._stops.get(binding_id)
        if stop:
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
        if not thread:
            return True
        thread.join(timeout=max(0.0, timeout_seconds))
        return not thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._reconcile_thread and self._reconcile_thread.is_alive():
                return
            self._reconcile_stop.clear()
            self._reconcile_thread = threading.Thread(
                target=self._reconcile_loop,
                name="staffdeck-dingtalk-reconcile",
                daemon=True,
            )
            self._reconcile_thread.start()

    def _reconcile_loop(self) -> None:
        while not self._reconcile_stop.wait(5.0):
            try:
                with Session(self._engine) as db:
                    active = {
                        row.id
                        for row in db.exec(
                            select(ChannelBinding).where(
                                ChannelBinding.channel == "dingtalk",
                                ChannelBinding.status == "active",
                            )
                        ).all()
                    }
                for binding_id in active:
                    self.ensure_binding(binding_id)
                with self._lock:
                    stale = set(self._threads) - active
                for binding_id in stale:
                    self.stop_binding(binding_id)
            except Exception:
                logger.exception("钉钉绑定 reconcile 失败")

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        self._reconcile_stop.set()
        reconcile_thread = self._reconcile_thread
        if reconcile_thread and reconcile_thread.is_alive():
            reconcile_thread.join(timeout=max(0.0, timeout_seconds))
        with self._lock:
            ids = list(self._threads)
        for binding_id in ids:
            self.stop_binding(binding_id)
        return all(self.wait_binding_stopped(binding_id, timeout_seconds) for binding_id in ids) and not (
            reconcile_thread and reconcile_thread.is_alive()
        )


register_channel_adapter("dingtalk", DingTalkAdapter())
