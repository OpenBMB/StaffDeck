from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any

from sqlmodel import Session, select

from app.channels.adapters.base import (
    ChannelInbound,
    register_channel_adapter,
    split_channel_text,
)
from app.channels.crypto import decrypt_channel_secret
from app.db import engine
from app.db.models import ChannelBinding, utc_now

logger = logging.getLogger(__name__)

RECONCILE_SECONDS = 30.0
SEND_TIMEOUT_SECONDS = 15.0


def is_self_frame(frame: dict[str, Any]) -> bool:
    """机器人自身发送的消息回调(from.userid == aibotid)。"""
    body = frame.get("body") or {}
    sender = str((body.get("from") or {}).get("userid") or "").strip()
    bot_id = str(body.get("aibotid") or "").strip()
    return bool(sender) and bool(bot_id) and sender == bot_id


def normalize_wecom_frame(frame: dict[str, Any], *, account_scope: str = "") -> ChannelInbound | None:
    """归一化企微 WS 消息帧；自身消息/非文本语音/缺字段返回 None（丢弃）。"""
    if not isinstance(frame, dict) or is_self_frame(frame):
        return None
    body = frame.get("body") or {}
    msgtype = str(body.get("msgtype") or "")
    text = ""
    if msgtype == "text":
        text = str((body.get("text") or {}).get("content") or "").strip()
    elif msgtype == "voice":
        # 语音帧 body.voice.content 为微信侧转写文本
        text = str((body.get("voice") or {}).get("content") or "").strip()
    if not text:
        return None
    from_user_id = str((body.get("from") or {}).get("userid") or "").strip()
    if not from_user_id:
        return None
    chat_id = str(body.get("chatid") or "").strip()
    chattype = str(body.get("chattype") or "").strip()
    if chattype == "group" and not chat_id:
        # 群消息缺 chatid 时按私聊降级,避免群会话退化为每人一个会话
        logger.warning("企微群消息缺少 chatid,按私聊降级处理 msgid=%s", body.get("msgid"))
        chattype = "single"
    # 官方文档：chatid 仅群聊返回
    is_group = bool(chat_id)
    headers = frame.get("headers") or {}
    event_id = str(body.get("msgid") or body.get("msg_id") or headers.get("req_id") or "").strip()
    if not event_id:
        return None
    sender_name = str((body.get("from") or {}).get("name") or body.get("name") or "").strip()
    return ChannelInbound(
        channel="wecom",
        event_id=event_id,
        from_user_id=from_user_id,
        to_user_id=str(body.get("aibotid") or "").strip(),
        session_id=chat_id or from_user_id,
        group_id=chat_id,
        # 企微无 context_token 概念：发送仅需 chatid，占位保持内核必填语义
        context_token=chat_id or from_user_id,
        text=text,
        is_group=is_group,
        raw=frame,
        sender_name=sender_name,
        account_scope=account_scope,
    )


def _default_client_factory(bot_id: str, secret: str):
    from aibot import WSClient, WSClientOptions

    return WSClient(WSClientOptions(bot_id=bot_id, secret=secret, max_reconnect_attempts=-1))


class _StreamState:
    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        # 每绑定一个入站工作线程:WS loop 线程只入队,AgentLoop 轮在 worker 里跑,心跳不被阻塞
        self.worker: threading.Thread | None = None
        self.stop = threading.Event()
        # join 超时后的强制退役:worker 完成当前条后退出并清理(不再接新消息)
        self.retired = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: Any = None
        self.queue: queue.Queue = queue.Queue()
        self.config_revision: int | None = None
        self.callback_condition = threading.Condition()
        self.callbacks_inflight = 0
        self.worker_stop_sent = False
        self.disconnect_sent = False


class WeComStreamManager:
    """每个 active 企微绑定一个专属线程跑独立 event loop + WSClient，reconcile 热启停。"""

    def __init__(
        self,
        *,
        db_engine=None,
        client_factory=None,
        reconcile_seconds: float = RECONCILE_SECONDS,
    ):
        self._engine = db_engine or engine
        self._client_factory = client_factory or _default_client_factory
        self._reconcile_seconds = reconcile_seconds
        self._streams: dict[str, _StreamState] = {}
        self._paused: set[str] = set()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._reconcile_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            return
        self._stopped.clear()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop,
            name="staffdeck-wecom-reconcile",
            daemon=True,
        )
        self._reconcile_thread.start()

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        self._stopped.set()
        with self._lock:
            states = list(self._streams.values())
            reconcile_thread = self._reconcile_thread
        for state in states:
            state.stop.set()
            self._stop_loop(state)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        for state in states:
            thread = state.thread
            if thread and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if not (thread and thread.is_alive()):
                self._signal_worker_after_producers(state, deadline)
            worker = state.worker
            if worker and worker.is_alive():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if worker and worker.is_alive():
                # join 超时:强制退役,worker 完成当前条后自行退出清理
                state.retired.set()
        if reconcile_thread and reconcile_thread.is_alive():
            reconcile_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return all(
            not (state.thread and state.thread.is_alive())
            and not (state.worker and state.worker.is_alive())
            for state in states
        ) and not (reconcile_thread and reconcile_thread.is_alive())

    def ensure_binding(self, binding_id: str) -> None:
        with Session(self._engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            if not binding or binding.status != "active":
                return
            config_revision = binding.config_revision
        with self._lock:
            if binding_id in self._paused:
                return
            state = self._streams.get(binding_id)
            if state and state.thread and state.thread.is_alive():
                return
            if state and state.worker and state.worker.is_alive():
                # stream 死 + worker 活:视为待回收——强制退役让 worker 完成当前条后
                # 退出并清理(退出路径会从 _streams 清除该 state),本轮不新建,
                # 下轮 reconcile 即可正常重建
                state.retired.set()
                return
            state = _StreamState()
            state.config_revision = config_revision
            state.worker = threading.Thread(
                target=self._run_worker,
                args=(binding_id, state),
                name=f"staffdeck-wecom-worker-{binding_id}",
                daemon=True,
            )
            thread = threading.Thread(
                target=self._run_stream,
                args=(binding_id, state),
                name=f"staffdeck-wecom-stream-{binding_id}",
                daemon=True,
            )
            state.thread = thread
            self._streams[binding_id] = state
            state.worker.start()
            thread.start()

    def stop_binding(self, binding_id: str) -> None:
        with self._lock:
            state = self._streams.get(binding_id)
        if state:
            state.stop.set()
            # 这里只拒绝新 callback 并停止 producer；worker sentinel 必须等 producer barrier。
            self._stop_loop(state)

    def pause_binding(self, binding_id: str) -> None:
        with self._lock:
            self._paused.add(binding_id)
        self.stop_binding(binding_id)

    def resume_binding(self, binding_id: str, *, start: bool = True) -> None:
        with self._lock:
            self._paused.discard(binding_id)
        if start:
            self.ensure_binding(binding_id)

    def _join_worker(self, state: _StreamState) -> None:
        worker = state.worker
        if worker and worker.is_alive():
            # 容忍超时:进行中的对话轮可能很长,worker 是 daemon,不阻塞停机
            worker.join(timeout=5.0)

    def wait_binding_stopped(self, binding_id: str, timeout_seconds: float = 5.0) -> bool:
        """有界等待 stream/worker 线程退出(重配凭证前调用),返回是否已停止。"""
        with self._lock:
            state = self._streams.get(binding_id)
        if not state:
            return True
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        thread = state.thread
        while thread and thread.is_alive():
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            # 覆盖 loop 发布/启动边界上的 stop 调度竞态，并给 worker 留出 deadline。
            self._stop_loop(state)
            thread.join(timeout=min(0.1, remaining))
        if thread and thread.is_alive():
            return False
        if not self._signal_worker_after_producers(state, deadline):
            # worker 在 deadline 内未收尾:强制退役,完成当前条后自行退出清理
            state.retired.set()
            return False
        worker = state.worker
        if worker and worker.is_alive():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if worker and worker.is_alive():
            # join 超时:强制退役,不挂死后续重建
            state.retired.set()
            return False
        return True

    @staticmethod
    def _signal_worker_after_producers(
        state: _StreamState,
        deadline: float | None,
    ) -> bool:
        with state.callback_condition:
            while state.callbacks_inflight:
                if deadline is None:
                    state.callback_condition.wait()
                    continue
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    return False
                state.callback_condition.wait(timeout=remaining)
            if not state.worker_stop_sent:
                state.queue.put_nowait(None)
                state.worker_stop_sent = True
            return True

    def running_binding_ids(self) -> set[str]:
        with self._lock:
            return {
                binding_id
                for binding_id, state in self._streams.items()
                if (state.thread and state.thread.is_alive())
                or (state.worker and state.worker.is_alive())
            }

    def get_stream(self, binding_id: str):
        """出站发送用:返回 (client, loop),未就绪返回 None。"""
        with self._lock:
            state = self._streams.get(binding_id)
        if state and state.client is not None and state.loop is not None and state.loop.is_running():
            return state.client, state.loop
        return None

    def reconcile_once(self) -> None:
        """对比 DB 中 active 企微绑定与运行中线程，热启停 + connected 状态对账。"""
        with Session(self._engine) as db:
            rows = db.exec(
                select(ChannelBinding).where(
                    ChannelBinding.channel == "wecom",
                    ChannelBinding.status == "active",
                )
            ).all()
        active_ids = {row.id for row in rows}
        with self._lock:
            active_ids -= self._paused
        for binding_id in active_ids - self.running_binding_ids():
            self.ensure_binding(binding_id)
        for binding_id in self.running_binding_ids() - active_ids:
            self.stop_binding(binding_id)
        # connected 对账:运行中按 SDK 实况对齐(无变化时 _set_connected 不写库),未运行置 False
        running_ids = self.running_binding_ids()
        for row in rows:
            with self._lock:
                state = self._streams.get(row.id)
                state_revision = state.config_revision if state else None
            if (
                row.id in running_ids
                and state_revision is not None
                and state_revision != row.config_revision
            ):
                self.stop_binding(row.id)
                self._set_connected(
                    row.id,
                    False,
                    config_revision=row.config_revision,
                )
                continue
            self._set_connected(
                row.id,
                self._stream_connected(row.id) if row.id in running_ids else False,
                config_revision=row.config_revision,
            )

    def _stream_connected(self, binding_id: str) -> bool:
        """运行中绑定的 SDK 实况连接状态(client 缺失或未暴露 is_connected 视为 False)。"""
        with self._lock:
            state = self._streams.get(binding_id)
        client = state.client if state else None
        if client is None:
            return False
        return bool(getattr(client, "is_connected", False))

    def _reconcile_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                self.reconcile_once()
            except Exception:
                logger.exception("企微 stream reconcile 失败")
            self._stopped.wait(self._reconcile_seconds)

    def _stop_loop(self, state: _StreamState) -> None:
        loop = state.loop
        client = state.client
        if loop is None or loop.is_closed():
            return
        try:
            with state.callback_condition:
                should_disconnect = client is not None and not state.disconnect_sent
                if should_disconnect:
                    state.disconnect_sent = True
            if should_disconnect:
                # SDK v1.0.2 的 disconnect 是同步方法,须调度到 loop 线程执行
                loop.call_soon_threadsafe(client.disconnect)
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            logger.debug("企微 disconnect 调度失败(忽略)", exc_info=True)

    def _set_connected(
        self,
        binding_id: str,
        connected: bool,
        *,
        config_revision: int | None = None,
    ) -> None:
        try:
            with Session(self._engine) as db:
                binding = db.get(ChannelBinding, binding_id)
                if not binding:
                    return
                if config_revision is not None and binding.config_revision != config_revision:
                    return
                if binding.connected != connected:
                    binding.connected = connected
                    binding.updated_at = utc_now()
                    db.add(binding)
                    db.commit()
        except Exception:
            logger.exception("企微连接状态落库失败 binding=%s", binding_id)

    def _wire_client(self, binding_id: str, client, state: _StreamState, account_scope: str = "") -> None:
        def on_authenticated(*_args) -> None:
            self._set_connected(
                binding_id,
                True,
                config_revision=state.config_revision,
            )

        def on_disconnected(*_args) -> None:
            self._set_connected(
                binding_id,
                False,
                config_revision=state.config_revision,
            )

        def on_frame(frame, *_args) -> None:
            # WS loop 线程只做归一化+入队,立即返回继续心跳;AgentLoop 轮在 worker 线程执行
            with state.callback_condition:
                if state.stop.is_set():
                    return
                state.callbacks_inflight += 1
            try:
                inbound = normalize_wecom_frame(frame, account_scope=account_scope)
                if inbound is None:
                    return
                state.queue.put_nowait((state.config_revision, inbound))
            except Exception:
                logger.exception("企微入站消息入队失败 binding=%s", binding_id)
            finally:
                with state.callback_condition:
                    state.callbacks_inflight -= 1
                    state.callback_condition.notify_all()

        client.on("authenticated", on_authenticated)
        client.on("disconnected", on_disconnected)
        client.on("message", on_frame)

    def _run_worker(self, binding_id: str, state: _StreamState) -> None:
        """单 worker 串行消费本绑定入站消息(与同会话串行锁语义一致)。"""
        from app.channels.service_intake import process_inbound

        try:
            while True:
                # 强制退役:完成当前条后退出,不再接新消息
                if state.retired.is_set():
                    return
                item = state.queue.get()
                if item is None:
                    return
                if isinstance(item, tuple) and len(item) == 2:
                    item_revision, inbound = item
                else:
                    item_revision, inbound = state.config_revision, item
                try:
                    with Session(self._engine) as db:
                        binding = db.get(ChannelBinding, binding_id)
                        if (
                            not binding
                            or binding.status != "active"
                            or binding.config_revision != item_revision
                        ):
                            continue
                        db.expunge(binding)
                    process_inbound(binding, inbound, db_engine=self._engine)
                except Exception:
                    logger.exception("企微入站消息处理失败 binding=%s", binding_id)
        finally:
            # 退出路径清理死 state(identity 守卫,不清除已被替换的新 state),
            # 下轮 reconcile 才能正常重建
            with self._lock:
                current = self._streams.get(binding_id)
                if current is state and not (state.thread and state.thread.is_alive()):
                    del self._streams[binding_id]

    def _run_stream(self, binding_id: str, state: _StreamState) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # 先发布 loop 再检查 stop，覆盖 ensure_binding 后立即 stop 的启动竞态。
        # stop 若更早发生，Event 会阻止连接；若更晚发生，则可调度 loop.stop。
        state.loop = loop
        try:
            if state.stop.is_set() or self._stopped.is_set():
                return
            with Session(self._engine) as db:
                binding = db.get(ChannelBinding, binding_id)
                if not binding or binding.status != "active":
                    return
                config = dict(binding.config_json or {})
                bot_id = str(config.get("bot_id") or "")
                secret = (
                    decrypt_channel_secret(binding.credentials_enc) if binding.credentials_enc else ""
                )
                if state.config_revision != binding.config_revision:
                    return
            if not bot_id or not secret:
                logger.warning("企微绑定缺少凭证,stream 退出 binding=%s", binding_id)
                return
            from app.channels.service_identity import external_account_scope

            account_scope = external_account_scope(None, binding)
            client = self._client_factory(bot_id, secret)
            self._wire_client(binding_id, client, state, account_scope)
            state.client = client
            if state.stop.is_set() or self._stopped.is_set():
                return
            loop.run_until_complete(client.connect())
            loop.run_forever()
        except Exception:
            logger.exception("企微 stream 线程异常 binding=%s", binding_id)
        finally:
            # 封闭 producer 注册窗口后再等待已登记 callback，sentinel 后不得再入队。
            state.stop.set()
            state.loop = None
            state.client = None
            self._signal_worker_after_producers(state, None)
            try:
                loop.close()
            except Exception:
                pass


class WeComAdapter:
    """企微适配器:归一化 + 出站 send_message(run_coroutine_threadsafe)+ ingress。

    官方 SDK 无 typing 能力,故不实现 send_typing。
    """

    def normalize(self, raw: dict[str, Any]) -> ChannelInbound | None:
        return normalize_wecom_frame(raw)

    def send(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        text: str,
        *,
        dedupe_key: str | None = None,
    ) -> None:
        chat_id = str(target.get("to_user_id") or "").strip()
        if not chat_id:
            raise ValueError("企微投递目标缺少 to_user_id(chatid)")
        # dedupe_key:企微 WS 协议的 req_id 由 SDK 内部生成,无客户端幂等标识可注入,仅保留签名一致
        from app.channels import get_wecom_stream_manager

        stream = get_wecom_stream_manager().get_stream(binding.id)
        if not stream:
            raise RuntimeError(f"企微连接未就绪 binding={binding.id}")
        client, loop = stream
        for chunk in split_channel_text(text):
            body = {"msgtype": "markdown", "markdown": {"content": chunk}}
            future = asyncio.run_coroutine_threadsafe(client.send_message(chat_id, body), loop)
            future.result(timeout=SEND_TIMEOUT_SECONDS)

    def start_ingress(self, binding_id: str) -> None:
        from app.channels import get_wecom_stream_manager

        get_wecom_stream_manager().ensure_binding(binding_id)

    def stop_ingress(self, binding_id: str) -> None:
        from app.channels import get_wecom_stream_manager

        get_wecom_stream_manager().stop_binding(binding_id)


# 模块导入即注册企微适配器(渠道内核按注册表发现渠道)
register_channel_adapter("wecom", WeComAdapter())
