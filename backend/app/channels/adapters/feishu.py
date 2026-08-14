from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable

import httpx

from app.channels.adapters.base import (
    CHANNEL_TEXT_LIMIT,
    ChannelInboundAttachment,
    register_channel_adapter,
    split_channel_text,
    stream_download_with_limit,
)
from app.channels.crypto import decrypt_channel_secret
from app.channels.markdown_render import (
    has_markdown,
    parse_markdown,
    render_feishu_post,
    split_markdown_by_lines,
)
from app.config import get_settings
from app.db.models import ChannelBinding

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
TOKEN_REFRESH_SKEW_SECONDS = 300
FEISHU_REACTION_TOKEN = "Get"
_TOKEN_INVALID_CODES = {99991663, 99991664, 99991668}
_PERMANENT_MESSAGE_CODES = {
    230001,  # invalid request/target
    230002,  # bot is not in the chat
    230006,  # message cannot be replied to
    230011,  # bot has no permission in the chat
}


class FeishuSendError(RuntimeError):
    retryable = True


class FeishuPermanentError(FeishuSendError):
    retryable = False


class FeishuTransientError(FeishuSendError):
    retryable = True


class FeishuTokenProvider:
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
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and not force_refresh and cached[1] > now:
                return cached[0]
            observed_token = cached[0] if cached else None
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                cached = self._cache.get(key)
                if cached and cached[1] > time.monotonic() and (
                    not force_refresh or cached[0] != observed_token
                ):
                    return cached[0]
            config = dict(binding.config_json or {})
            app_id = str(config.get("app_id") or "").strip()
            if not app_id or not binding.credentials_enc:
                raise FeishuPermanentError("飞书绑定缺少应用凭证")
            app_secret = decrypt_channel_secret(binding.credentials_enc)
            try:
                with self._client_factory() as client:
                    response = client.post(
                        f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
                        json={"app_id": app_id, "app_secret": app_secret},
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise FeishuTransientError("飞书 token 请求暂时失败") from exc
            try:
                data = response.json()
            except ValueError as exc:
                raise FeishuTransientError("飞书 token 响应格式无效") from exc
            if response.status_code >= 500 or response.status_code == 429:
                raise FeishuTransientError("飞书 token 服务暂时不可用")
            if response.status_code >= 400 or int(data.get("code", -1)) != 0:
                raise FeishuPermanentError("飞书应用凭证无效或无权限")
            token = str(data.get("tenant_access_token") or "").strip()
            expires_in = int(data.get("expire") or 0)
            if not token or expires_in <= 0:
                raise FeishuTransientError("飞书 token 响应缺少必要字段")
            valid_for = max(1, expires_in - TOKEN_REFRESH_SKEW_SECONDS)
            with self._lock:
                self._cache[key] = (token, time.monotonic() + valid_for)
            return token


def validate_feishu_credentials(
    app_id: str,
    app_secret: str,
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, str]:
    factory = client_factory or (lambda: httpx.Client(timeout=10.0))
    try:
        with factory() as client:
            token_response = client.post(
                f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            token_data = token_response.json()
            token = str(token_data.get("tenant_access_token") or "").strip()
            if token_response.status_code == 429 or token_response.status_code >= 500:
                raise FeishuTransientError("飞书 token 服务暂时不可用")
            if (
                token_response.status_code >= 400
                or int(token_data.get("code", -1)) != 0
                or not token
            ):
                raise FeishuPermanentError("飞书应用凭证无效或无权限")
            bot_response = client.get(
                f"{FEISHU_API_BASE}/bot/v3/info/",
                headers={"Authorization": f"Bearer {token}"},
            )
            bot_data = bot_response.json()
    except FeishuSendError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise FeishuTransientError("无法验证飞书应用凭证") from exc
    if bot_response.status_code == 429 or bot_response.status_code >= 500:
        raise FeishuTransientError("飞书机器人信息服务暂时不可用")
    if bot_response.status_code >= 400 or int(bot_data.get("code", -1)) != 0:
        raise FeishuPermanentError("无法读取飞书机器人信息，请检查应用权限")
    bot = bot_data.get("bot") or {}
    open_id = str(bot.get("open_id") or "").strip()
    if not open_id:
        raise FeishuPermanentError("飞书机器人信息缺少 open_id")
    return {"bot_open_id": open_id, "bot_name": str(bot.get("app_name") or "").strip()}


class FeishuAdapter:
    # 飞书 reaction 会返回远端 reaction_id，重复挂会产生第二个表情，因此重试前必须
    # 先用 find_own_reaction() 回查，不能直接重发。
    reaction_attach_idempotent = False
    reaction_token = FEISHU_REACTION_TOKEN

    def __init__(
        self,
        *,
        token_provider: FeishuTokenProvider | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=15.0))
        self._tokens = token_provider or FeishuTokenProvider(client_factory=self._client_factory)

    def normalize(self, raw: dict[str, Any]):
        return None

    @staticmethod
    def _uuid(idempotency_key: str, chunk_index: int) -> str:
        return hashlib.sha256(
            f"{idempotency_key}:{chunk_index}".encode("utf-8")
        ).hexdigest()[:40]

    def _request(
        self,
        binding: ChannelBinding,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        force_refresh = False
        for attempt in range(2):
            token = self._tokens.get(binding, force_refresh=force_refresh)
            force_refresh = False
            try:
                with self._client_factory() as client:
                    request_kwargs = {
                        "params": params,
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                    if method == "POST":
                        response = client.post(url, json=body, **request_kwargs)
                    elif method == "GET":
                        response = client.get(url, **request_kwargs)
                    elif method == "PATCH":
                        response = client.patch(url, json=body, **request_kwargs)
                    else:
                        response = client.delete(url, **request_kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise FeishuTransientError("飞书消息发送暂时失败") from exc
            if response.status_code == 401 and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise FeishuTransientError("飞书消息服务暂时不可用")
            if method == "DELETE" and response.status_code == 404:
                return {}
            try:
                data = response.json()
            except ValueError as exc:
                raise FeishuTransientError("飞书消息响应格式无效") from exc
            if response.status_code >= 400:
                raise FeishuPermanentError(f"飞书拒绝消息请求 HTTP {response.status_code}")
            code = int(data.get("code", -1))
            if code in _TOKEN_INVALID_CODES and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if code in _TOKEN_INVALID_CODES:
                raise FeishuPermanentError("飞书 token 刷新后仍无效")
            if code in _PERMANENT_MESSAGE_CODES:
                raise FeishuPermanentError(
                    f"飞书拒绝消息请求 code={code}"
                )
            if code != 0:
                raise FeishuTransientError(f"飞书消息服务返回错误 code={code}")
            return data
        raise FeishuPermanentError("飞书 token 刷新后仍无效")

    def _post(
        self,
        binding: ChannelBinding,
        url: str,
        *,
        params: dict[str, str] | None,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(binding, "POST", url, params=params, body=body)

    def add_reaction(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        emoji_type: str = FEISHU_REACTION_TOKEN,
    ) -> str:
        message_id = str((target or {}).get("message_id") or "").strip()
        emoji_type = str(emoji_type or "").strip()
        if not message_id or not emoji_type:
            raise FeishuPermanentError("飞书 reaction 参数无效")
        data = self._post(
            binding,
            f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions",
            params=None,
            body={"reaction_type": {"emoji_type": emoji_type}},
        )
        reaction_id = str((data.get("data") or {}).get("reaction_id") or "").strip()
        if not reaction_id:
            raise FeishuTransientError("飞书 reaction 响应缺少 reaction_id")
        return reaction_id

    def find_own_reaction(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        emoji_type: str = FEISHU_REACTION_TOKEN,
    ) -> str | None:
        message_id = str((target or {}).get("message_id") or "").strip()
        emoji_type = str(emoji_type or "").strip()
        app_id = str((binding.config_json or {}).get("app_id") or "").strip()
        if not message_id or not emoji_type or not app_id:
            raise FeishuPermanentError("飞书 reaction 查询参数无效")
        page_token = ""
        for _ in range(20):
            params = {"reaction_type": emoji_type, "page_size": "50"}
            if page_token:
                params["page_token"] = page_token
            data = self._request(
                binding,
                "GET",
                f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions",
                params=params,
                body=None,
            )
            payload = data.get("data") or {}
            for item in payload.get("items") or []:
                operator = item.get("operator") or {}
                reaction_type = item.get("reaction_type") or {}
                if (
                    operator.get("operator_type") == "app"
                    and str(operator.get("operator_id") or "") == app_id
                    and str(reaction_type.get("emoji_type") or "") == emoji_type
                ):
                    reaction_id = str(item.get("reaction_id") or "").strip()
                    if reaction_id:
                        return reaction_id
            if not payload.get("has_more"):
                return None
            page_token = str(payload.get("page_token") or "").strip()
            if not page_token:
                raise FeishuTransientError("飞书 reaction 分页响应无效")
        raise FeishuTransientError("飞书 reaction 分页数量异常")

    def remove_reaction(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        reaction_id: str,
    ) -> None:
        message_id = str(target.get("message_id") or "").strip()
        reaction_id = str(reaction_id or "").strip()
        if not message_id or not reaction_id:
            raise FeishuPermanentError("飞书 reaction 清理参数无效")
        self._request(
            binding,
            "DELETE",
            f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions/{reaction_id}",
            params=None,
            body=None,
        )

    def download_media(
        self,
        binding: ChannelBinding,
        attachment: ChannelInboundAttachment,
        *,
        max_bytes: int = 0,
    ) -> bytes:
        """飞书附件下载:im/v1/messages/{message_id}/resources/{file_key}?type=image|file。

        download_params 中需含 file_key / type / message_id。
        下载返回二进制流,不走 _request(那个解析 JSON),直接使用 response.content。
        复用 FeishuTokenProvider 的 token 刷新重试模式(401 -> invalidate -> 重试一次)。
        max_bytes > 0 时流式读取,超过上限立即中止并抛 ValueError。
        """
        file_key = str(attachment.download_params.get("file_key") or attachment.media_id)
        media_type = str(attachment.download_params.get("type") or "").strip()
        message_id = str(attachment.download_params.get("message_id") or "").strip()
        if not file_key or not media_type or not message_id:
            raise FeishuPermanentError(
                f"飞书附件下载参数缺失: file_key={file_key} type={media_type} message_id={message_id}"
            )
        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/resources/{file_key}"
        force_refresh = False
        for attempt in range(2):
            token = self._tokens.get(binding, force_refresh=force_refresh)
            force_refresh = False
            try:
                with self._client_factory() as client:
                    if max_bytes > 0:
                        status, data = stream_download_with_limit(
                            client, "GET", url,
                            params={"type": media_type},
                            headers={"Authorization": f"Bearer {token}"},
                            max_bytes=max_bytes,
                        )
                        if status == 401 and attempt == 0:
                            force_refresh = self._tokens.invalidate(binding, expected_token=token)
                            continue
                        if status == 429 or status >= 500:
                            raise FeishuTransientError("飞书附件下载服务暂时不可用")
                        if status >= 400:
                            raise FeishuPermanentError(f"飞书拒绝附件下载 HTTP {status}")
                        return data
                    response = client.get(
                        url,
                        params={"type": media_type},
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except ValueError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise FeishuTransientError("飞书附件下载暂时失败") from exc
            if response.status_code == 401 and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise FeishuTransientError("飞书附件下载服务暂时不可用")
            if response.status_code >= 400:
                raise FeishuPermanentError(f"飞书拒绝附件下载 HTTP {response.status_code}")
            return response.content
        raise FeishuPermanentError("飞书 token 刷新后仍无法下载附件")

    def create_card(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        card_json: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> str:
        """发送一张交互式卡片，返回 message_id。

        复用 send() 的目标解析逻辑：有 message_id 走 reply，否则按 receive_id 投递。
        idempotency_key 生成稳定 uuid，保证重试不重复发卡。
        """
        key = str(idempotency_key or "").strip()
        if not key:
            raise FeishuPermanentError("飞书卡片创建缺少幂等键")
        message_id = str(target.get("message_id") or "").strip()
        receive_id = str(target.get("receive_id") or "").strip()
        receive_id_type = str(target.get("receive_id_type") or "").strip()
        if not message_id and (not receive_id or not receive_id_type):
            raise FeishuPermanentError("飞书卡片投递目标无效")
        body: dict[str, Any] = {
            "msg_type": "interactive",
            "content": json.dumps(card_json, ensure_ascii=False),
            "uuid": self._uuid(key, 0),
        }
        if message_id:
            body["reply_in_thread"] = bool(target.get("reply_in_thread"))
            data = self._post(
                binding,
                f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply",
                params=None,
                body=body,
            )
        else:
            body["receive_id"] = receive_id
            data = self._post(
                binding,
                f"{FEISHU_API_BASE}/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                body=body,
            )
        created_id = str((data.get("data") or {}).get("message_id") or "").strip()
        if not created_id:
            raise FeishuTransientError("飞书卡片创建响应缺少 message_id")
        return created_id

    def update_card(
        self,
        binding: ChannelBinding,
        message_id: str,
        card_json: dict[str, Any],
    ) -> None:
        """PATCH 更新已发送卡片的 content。"""
        message_id = str(message_id or "").strip()
        if not message_id:
            raise FeishuPermanentError("飞书卡片更新缺少 message_id")
        self._request(
            binding,
            "PATCH",
            f"{FEISHU_API_BASE}/im/v1/messages/{message_id}",
            params=None,
            body={"content": json.dumps(card_json, ensure_ascii=False)},
        )

    def send(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        key = str(idempotency_key or "").strip()
        if not str(text or "").strip():
            raise FeishuPermanentError("飞书投递文本不能为空")
        if not key:
            raise FeishuPermanentError("飞书投递缺少幂等键")
        message_id = str(target.get("message_id") or "").strip()
        receive_id = str(target.get("receive_id") or "").strip()
        receive_id_type = str(target.get("receive_id_type") or "").strip()
        if not message_id and (not receive_id or not receive_id_type):
            raise FeishuPermanentError("飞书投递目标无效")
        rich_enabled = bool(get_settings().channel_rich_render_enabled)
        use_rich = rich_enabled and has_markdown(text)
        if use_rich:
            chunks = split_markdown_by_lines(text, CHANNEL_TEXT_LIMIT)
            if not chunks:
                chunks = [text]
        else:
            chunks = split_channel_text(text)
        for index, chunk in enumerate(chunks):
            if use_rich:
                post_content = render_feishu_post(parse_markdown(chunk))
                body: dict[str, Any] = {
                    "msg_type": "post",
                    "content": json.dumps(post_content, ensure_ascii=False),
                    "uuid": self._uuid(key, index),
                }
            else:
                body = {
                    "msg_type": "text",
                    "content": json.dumps({"text": chunk}, ensure_ascii=False),
                    "uuid": self._uuid(key, index),
                }
            if message_id:
                body["reply_in_thread"] = bool(target.get("reply_in_thread"))
                self._post(
                    binding,
                    f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply",
                    params=None,
                    body=body,
                )
            else:
                body["receive_id"] = receive_id
                self._post(
                    binding,
                    f"{FEISHU_API_BASE}/im/v1/messages",
                    params={"receive_id_type": receive_id_type},
                    body=body,
                )

    def start_ingress(self, binding_id: str) -> None:
        from app.channels import get_feishu_process_manager

        get_feishu_process_manager().ensure_binding(binding_id)

    def stop_ingress(self, binding_id: str) -> None:
        from app.channels import get_feishu_process_manager

        get_feishu_process_manager().stop_binding(binding_id)


register_channel_adapter("feishu", FeishuAdapter())
