from __future__ import annotations

import base64
import logging
import os
import threading
import time
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
from sqlmodel import Session, select

from app.channels.adapters.base import (
    ChannelInbound,
    register_channel_adapter,
    split_channel_text,
)
from app.channels.crypto import decrypt_channel_secret
from app.config import get_settings
from app.db import engine
from app.db.models import ChannelBinding, utc_now

logger = logging.getLogger(__name__)

CHANNEL_VERSION = "1.0.0"
WECHAT_TEXT_LIMIT = 2000
GETUPDATES_TIMEOUT_SECONDS = 40.0
SESSION_EXPIRED_ERRCODE = -14
POLL_BACKOFF_START_SECONDS = 2.0
# 腾讯保留限速权，微信渠道退避上限放宽到 60s
POLL_BACKOFF_MAX_SECONDS = 60.0
# 连续失败熔断：5 次后暂停 5 分钟
POLL_FAILURE_CIRCUIT_THRESHOLD = 5
POLL_FAILURE_CIRCUIT_SECONDS = 300.0
RECONCILE_SECONDS = 30.0
# -14 自愈策略(对齐官方插件):冷却后用原 token/原游标重试,默认 1 小时
RECOVERY_COOLDOWN_SECONDS = 3600.0
# 连续恢复失败达上限才判真过期(expired + 清游标 + 线程退出)
RECOVERY_MAX_FAILURES = 5

# 兼容旧名:统一内核类型后,微信入站消息即 ChannelInbound(channel="wechat")
WeChatInbound = ChannelInbound


class WeChatApiError(Exception):
    def __init__(self, errcode: int, message: str):
        super().__init__(f"微信 iLink 接口错误 errcode={errcode}: {message}")
        self.errcode = errcode


def random_wechat_uin() -> str:
    """X-WECHAT-UIN：随机 uint32 的十进制字符串再 base64，每次请求重新生成。"""
    value = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(value).encode("utf-8")).decode("utf-8")


def split_wechat_text(text: str, limit: int = WECHAT_TEXT_LIMIT) -> list[str]:
    """按 2000 字上限拆分长文本，优先 \n\n / \n / 空格边界，找不到则硬切。"""
    return split_channel_text(text, limit)


class WeChatClient:
    """腾讯 iLink 协议 HTTP 客户端（扫码绑定 + getupdates 收 + sendmessage 发）。"""

    def __init__(
        self,
        base_url: str,
        bot_token: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.bot_token = bot_token
        self._client = httpx.Client(transport=transport)

    @classmethod
    def for_binding(cls, binding: ChannelBinding) -> "WeChatClient":
        config = dict(binding.config_json or {})
        base_url = str(config.get("baseurl") or "").strip() or get_settings().wechat_ilink_base_url
        token = ""
        if binding.credentials_enc:
            token = decrypt_channel_secret(binding.credentials_enc)
        return cls(base_url, token)

    def _business_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.bot_token}",
            "X-WECHAT-UIN": random_wechat_uin(),
        }

    @staticmethod
    def _base_info() -> dict[str, Any]:
        return {"channel_version": CHANNEL_VERSION}

    def get_bot_qrcode(self, local_token_list: list[str] | None = None) -> dict[str, Any]:
        # local_token_list:本地已有 bot_token 列表(官方签名,最多 10 个)
        resp = self._client.post(
            f"{self.base_url}/ilink/bot/get_bot_qrcode",
            params={"bot_type": 3},
            json={"local_token_list": list(local_token_list or [])[:10]},
            timeout=20.0,
        )
        resp.raise_for_status()
        return dict(resp.json() or {})

    def get_qrcode_status(
        self,
        qrcode: str,
        *,
        verify_code: str | None = None,
        timeout_seconds: float = 35.0,
    ) -> dict[str, Any]:
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        resp = self._client.get(
            f"{self.base_url}/ilink/bot/get_qrcode_status",
            params=params,
            headers={"iLink-App-ClientVersion": "1"},
            timeout=timeout_seconds + 5.0,
        )
        resp.raise_for_status()
        return dict(resp.json() or {})

    def get_updates(
        self,
        get_updates_buf: str,
        *,
        timeout_seconds: float = GETUPDATES_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        resp = self._client.post(
            f"{self.base_url}/ilink/bot/getupdates",
            headers=self._business_headers(),
            json={"get_updates_buf": get_updates_buf, "base_info": self._base_info()},
            timeout=timeout_seconds + 5.0,
        )
        resp.raise_for_status()
        return dict(resp.json() or {})

    def send_message(self, to_user_id: str, context_token: str, text: str) -> None:
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"staffdeck:{int(time.time() * 1000)}:{uuid4().hex[:8]}",
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            },
            "base_info": self._base_info(),
        }
        resp = self._client.post(
            f"{self.base_url}/ilink/bot/sendmessage",
            headers=self._business_headers(),
            json=payload,
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        if isinstance(data, dict):
            errcode = data.get("errcode") or data.get("ret") or 0
            if errcode:
                raise WeChatApiError(int(errcode), str(data.get("errmsg") or ""))

    def get_config(self, ilink_user_id: str, context_token: str = "") -> dict[str, Any]:
        resp = self._client.post(
            f"{self.base_url}/ilink/bot/getconfig",
            headers=self._business_headers(),
            json={
                "ilink_user_id": ilink_user_id,
                "context_token": context_token,
                "base_info": self._base_info(),
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        return dict(resp.json() or {})

    def send_typing(self, ilink_user_id: str, typing_ticket: str, status: int = 1) -> None:
        # status: 1=正在输入 2=取消输入
        resp = self._client.post(
            f"{self.base_url}/ilink/bot/sendtyping",
            headers=self._business_headers(),
            json={
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
                "base_info": self._base_info(),
            },
            timeout=15.0,
        )
        resp.raise_for_status()


class WeChatAdapter:
    """微信适配器:出站 sendmessage + 归一化 + typing + ingress(poll manager)。"""

    def __init__(self, client_factory=None):
        self._client_factory = client_factory or WeChatClient.for_binding

    def normalize(self, raw: dict[str, Any]) -> ChannelInbound | None:
        return normalize_wechat_message(raw)

    def send(self, binding: ChannelBinding, target: dict[str, Any], text: str) -> None:
        to_user_id = str(target.get("to_user_id") or "").strip()
        context_token = str(target.get("context_token") or "").strip()
        if not to_user_id or not context_token:
            raise ValueError("微信投递目标缺少 to_user_id 或 context_token")
        client = self._client_factory(binding)
        for chunk in split_wechat_text(text):
            client.send_message(to_user_id, context_token, chunk)

    def send_typing(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        status: int,
        *,
        db_engine=None,
        client_factory=None,
    ) -> None:
        """best-effort 发送"正在输入"状态(status 1=typing 2=cancel),任何失败仅记日志。"""
        try:
            ilink_user_id = str(target.get("to_user_id") or "")
            context_token = str(target.get("context_token") or "")
            factory = client_factory or self._client_factory
            with Session(db_engine or engine) as db:
                row = db.get(ChannelBinding, binding.id)
                if not row or row.status != "active":
                    return
                config = dict(row.config_json or {})
                ticket = str(config.get("typing_ticket") or "")
                client = factory(row)
                if status == 1 and not ticket:
                    # get_config 失败就跳过:typing 是增强体验,不能阻塞主链路
                    ticket = str(client.get_config(ilink_user_id, context_token).get("typing_ticket") or "")
                    if not ticket:
                        return
                    config["typing_ticket"] = ticket
                    row.config_json = config
                    row.updated_at = utc_now()
                    db.add(row)
                    db.commit()
                if not ticket:
                    return
                try:
                    client.send_typing(ilink_user_id, ticket, status)
                except Exception:
                    # ticket 可能已失效:清掉缓存,下次重新获取
                    if "typing_ticket" in config:
                        config.pop("typing_ticket", None)
                        row.config_json = config
                        row.updated_at = utc_now()
                        db.add(row)
                        db.commit()
                    raise
        except Exception:
            logger.debug("微信 typing 状态发送失败(忽略) binding=%s status=%s", binding.id, status, exc_info=True)

    def start_ingress(self, binding_id: str) -> None:
        from app.channels import get_wechat_poll_manager

        get_wechat_poll_manager().ensure_binding(binding_id)

    def stop_ingress(self, binding_id: str) -> None:
        from app.channels import get_wechat_poll_manager

        get_wechat_poll_manager().stop_binding(binding_id)


def is_self_message(msg: dict[str, Any], ilink_bot_id: str = "") -> bool:
    if msg.get("message_type") == 2:
        return True
    from_user_id = str(msg.get("from_user_id") or "").strip()
    return bool(ilink_bot_id) and from_user_id == ilink_bot_id


def extract_message_text(msg: dict[str, Any]) -> str:
    items = msg.get("item_list")
    if isinstance(items, list):
        parts: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == 1:
                text_item = item.get("text_item")
                if isinstance(text_item, dict):
                    value = str(text_item.get("text") or "").strip()
                    if value:
                        parts.append(value)
            elif item.get("type") == 3:
                # 语音消息:微信侧已转好的文字在 voice_item.text
                voice_item = item.get("voice_item")
                if isinstance(voice_item, dict):
                    value = str(voice_item.get("text") or "").strip()
                    if value:
                        parts.append(value)
        if parts:
            return "\n".join(parts)
    return str(msg.get("text") or msg.get("content") or "").strip()


def normalize_wechat_message(msg: dict[str, Any], *, ilink_bot_id: str = "") -> WeChatInbound | None:
    """归一化 getupdates 消息；自身消息/无文本/无 context_token 返回 None（丢弃）。"""
    if not isinstance(msg, dict) or is_self_message(msg, ilink_bot_id):
        return None
    from_user_id = str(msg.get("from_user_id") or "").strip()
    if not from_user_id:
        return None
    context_token = str(msg.get("context_token") or "").strip()
    text = extract_message_text(msg)
    if not context_token or not text:
        return None
    event_id = str(msg.get("message_id") or msg.get("msg_id") or msg.get("client_id") or "").strip()
    if not event_id:
        return None
    session_id = str(msg.get("session_id") or "").strip()
    group_id = str(msg.get("group_id") or "").strip()
    # p2p 会话 session_id 形如 "user#bot"；群聊优先看 group_id，兜底用无 # 的 session_id
    is_group = bool(group_id) or (
        bool(session_id) and "#" not in session_id and session_id != from_user_id
    )
    return ChannelInbound(
        channel="wechat",
        event_id=event_id,
        from_user_id=from_user_id,
        to_user_id=str(msg.get("to_user_id") or "").strip(),
        session_id=session_id,
        group_id=group_id,
        context_token=context_token,
        text=text,
        is_group=is_group,
        raw=msg,
    )


class WeChatPollManager:
    """每个 active 绑定一个 getupdates 长轮询线程，reconcile 线程做热启停。"""

    def __init__(
        self,
        *,
        db_engine=None,
        client_factory=None,
        reconcile_seconds: float = RECONCILE_SECONDS,
        recovery_cooldown_seconds: float = RECOVERY_COOLDOWN_SECONDS,
    ):
        self._engine = db_engine or engine
        self._client_factory = client_factory or WeChatClient.for_binding
        self._reconcile_seconds = reconcile_seconds
        self._recovery_cooldown_seconds = recovery_cooldown_seconds
        self._threads: dict[str, threading.Thread] = {}
        self._stop_flags: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._reconcile_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            return
        self._stopped.clear()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop,
            name="staffdeck-wechat-reconcile",
            daemon=True,
        )
        self._reconcile_thread.start()

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            flags = list(self._stop_flags.values())
        for flag in flags:
            flag.set()

    def ensure_binding(self, binding_id: str) -> None:
        with self._lock:
            thread = self._threads.get(binding_id)
            if thread and thread.is_alive():
                return
            flag = threading.Event()
            self._stop_flags[binding_id] = flag
            thread = threading.Thread(
                target=self._poll_loop,
                args=(binding_id, flag),
                name=f"staffdeck-wechat-poll-{binding_id}",
                daemon=True,
            )
            self._threads[binding_id] = thread
            thread.start()

    def stop_binding(self, binding_id: str) -> None:
        with self._lock:
            flag = self._stop_flags.get(binding_id)
        if flag:
            flag.set()

    def running_binding_ids(self) -> set[str]:
        with self._lock:
            return {binding_id for binding_id, thread in self._threads.items() if thread.is_alive()}

    def reconcile_once(self) -> None:
        """对比 DB 中 active 绑定与运行中线程，热启停。"""
        with Session(self._engine) as db:
            rows = db.exec(
                select(ChannelBinding).where(
                    ChannelBinding.channel == "wechat",
                    ChannelBinding.status == "active",
                )
            ).all()
        active_ids = {row.id for row in rows}
        for binding_id in active_ids - self.running_binding_ids():
            self.ensure_binding(binding_id)
        for binding_id in self.running_binding_ids() - active_ids:
            self.stop_binding(binding_id)

    def _reconcile_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                self.reconcile_once()
            except Exception:
                logger.exception("微信 poll reconcile 失败")
            self._stopped.wait(self._reconcile_seconds)

    def _load_binding(self, binding_id: str) -> ChannelBinding | None:
        with Session(self._engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            if not binding or binding.status != "active":
                return None
            db.expunge(binding)
            return binding

    def _poll_loop(self, binding_id: str, stop_flag: threading.Event) -> None:
        backoff = POLL_BACKOFF_START_SECONDS
        failures = 0
        # 官方建议:优先使用服务端下发的 longpolling_timeout_ms(内存态跟踪,限 10-60s)
        poll_timeout = GETUPDATES_TIMEOUT_SECONDS
        while not stop_flag.is_set() and not self._stopped.is_set():
            try:
                binding = self._load_binding(binding_id)
                if not binding:
                    return
                config = dict(binding.config_json or {})
                cursor = str(config.get("get_updates_buf") or "")
                ilink_bot_id = str(config.get("ilink_bot_id") or "")
                client = self._client_factory(binding)
                try:
                    resp = client.get_updates(cursor, timeout_seconds=poll_timeout)
                    errcode = resp.get("errcode") or resp.get("ret") or 0
                    if errcode == SESSION_EXPIRED_ERRCODE:
                        # 自愈优先:不清游标、线程不死,冷却后原 token 重试;达上限才判真过期
                        if self._enter_session_recovery(binding_id, stop_flag):
                            continue
                        if not stop_flag.is_set() and not self._stopped.is_set():
                            logger.warning("微信会话恢复失败达上限(-14),判真过期 binding=%s", binding_id)
                            self._mark_session_expired(binding_id)
                        return
                    if errcode:
                        raise WeChatApiError(int(errcode), str(resp.get("errmsg") or ""))
                except Exception as exc:
                    failures += 1
                    backoff = self._on_failure(binding_id, stop_flag, failures, backoff, exc)
                    if failures >= POLL_FAILURE_CIRCUIT_THRESHOLD:
                        failures = 0
                    continue
                failures = 0
                backoff = POLL_BACKOFF_START_SECONDS
                self._clear_session_recovery(binding_id)
                timeout_ms = resp.get("longpolling_timeout_ms")
                if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
                    poll_timeout = min(max(float(timeout_ms) / 1000.0, 10.0), 60.0)
                new_cursor = str(resp.get("get_updates_buf") or "")
                if not self._persist_cursor(binding_id, new_cursor):
                    return
                from app.channels.service_intake import process_inbound

                for msg in resp.get("msgs") or []:
                    if not isinstance(msg, dict) or is_self_message(msg, ilink_bot_id):
                        continue
                    try:
                        process_inbound(binding, msg, db_engine=self._engine)
                    except Exception:
                        logger.exception("微信入站消息处理失败 binding=%s", binding_id)
            except Exception:
                logger.exception("微信 poll 线程异常 binding=%s", binding_id)
                stop_flag.wait(backoff)
                backoff = min(backoff * 2, POLL_BACKOFF_MAX_SECONDS)

    def _on_failure(
        self,
        binding_id: str,
        stop_flag: threading.Event,
        failures: int,
        backoff: float,
        exc: Exception,
    ) -> float:
        logger.warning("微信 getupdates 失败(连续 %s 次) binding=%s: %s", failures, binding_id, exc)
        if failures >= POLL_FAILURE_CIRCUIT_THRESHOLD:
            logger.warning("微信 getupdates 连续失败熔断 %ss binding=%s", POLL_FAILURE_CIRCUIT_SECONDS, binding_id)
            stop_flag.wait(POLL_FAILURE_CIRCUIT_SECONDS)
            return POLL_BACKOFF_START_SECONDS
        stop_flag.wait(backoff)
        return min(backoff * 2, POLL_BACKOFF_MAX_SECONDS)

    def _enter_session_recovery(self, binding_id: str, stop_flag: threading.Event) -> bool:
        """-14 自愈流程:记恢复状态(不清游标)→ 冷却等待(可被 stop 打断)→ 原 token 重试。

        返回 True=冷却结束继续重试;False=应退出(stop 打断或恢复失败达上限)。
        """
        with Session(self._engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            if not binding:
                return False
            config = dict(binding.config_json or {})
            failures = int(config.get("recovery_failures") or 0) + 1
            if failures >= RECOVERY_MAX_FAILURES:
                return False
            config["session_expired"] = True
            config["recovery_failures"] = failures
            config["next_recovery_at"] = (
                utc_now() + timedelta(seconds=self._recovery_cooldown_seconds)
            ).isoformat()
            binding.config_json = config
            binding.connected = False
            binding.updated_at = utc_now()
            db.add(binding)
            db.commit()
        logger.warning(
            "微信会话疑似过期(-14,第 %s/%s 次),冷却 %.0fs 后用原 token 重试 binding=%s",
            failures,
            RECOVERY_MAX_FAILURES,
            self._recovery_cooldown_seconds,
            binding_id,
        )
        stop_flag.wait(self._recovery_cooldown_seconds)
        return not stop_flag.is_set() and not self._stopped.is_set()

    def _clear_session_recovery(self, binding_id: str) -> None:
        """恢复重试成功:清 session_expired/计数/下次恢复时间(非恢复中不写库)。"""
        with Session(self._engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            if not binding:
                return
            config = dict(binding.config_json or {})
            if (
                not config.get("session_expired")
                and not config.get("recovery_failures")
                and "next_recovery_at" not in config
            ):
                return
            config["session_expired"] = False
            config["recovery_failures"] = 0
            config.pop("next_recovery_at", None)
            binding.config_json = config
            binding.updated_at = utc_now()
            db.add(binding)
            db.commit()

    def _persist_cursor(self, binding_id: str, new_cursor: str) -> bool:
        with Session(self._engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            if not binding or binding.status != "active":
                return False
            if new_cursor:
                config = dict(binding.config_json or {})
                config["get_updates_buf"] = new_cursor
                binding.config_json = config
            binding.connected = True
            binding.updated_at = utc_now()
            db.add(binding)
            db.commit()
            return True

    def _mark_session_expired(self, binding_id: str) -> None:
        with Session(self._engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            if not binding:
                return
            config = dict(binding.config_json or {})
            config["session_expired"] = True
            config["get_updates_buf"] = ""
            binding.config_json = config
            binding.status = "expired"
            binding.connected = False
            binding.updated_at = utc_now()
            db.add(binding)
            db.commit()


# 模块导入即注册微信适配器(渠道内核按注册表发现渠道)
register_channel_adapter("wechat", WeChatAdapter())
