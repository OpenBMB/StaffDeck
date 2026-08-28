from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import text, update
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from app.channels.adapters.base import (
    ChannelInbound,
    ChannelInboundAttachment,
    register_channel_adapter,
)
from app.channels.crypto import decrypt_channel_secret
from app.channels.media import (
    MAX_CHANNEL_MEDIA_BYTES,
    MAX_ENCRYPTED_CHANNEL_MEDIA_BYTES,
    ensure_channel_media_size,
)
from app.db import engine
from app.db.models import ChannelBinding, GeneralSkill, Skill, Tool, utc_now

logger = logging.getLogger(__name__)

RECONCILE_SECONDS = 30.0
SEND_TIMEOUT_SECONDS = 15.0
# 企微长连接持续未 connected 超过该阈值时,给绑定创建者发一次性断开告警
WECOM_DISCONNECT_ALERT_MINUTES = 15
WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
WECOM_TOKEN_REFRESH_SKEW_SECONDS = 300
WECOM_MEDIA_HOSTS = {"ww-aibot-img-1258476243.cos.ap-guangzhou.myqcloud.com"}
WECOM_STREAM_MIN_INTERVAL_SECONDS = 0.5
WECOM_PROGRESS_FRAMES = ("📖", "📗", "📘", "📙", "📕")
WECOM_PROCESSING_FRAMES = ("⏳", "⌛")
# The mobile client truncates long stream cards more aggressively than desktop.
WECOM_STREAM_MAX_BYTES = 1800


def validate_wecom_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in WECOM_MEDIA_HOSTS


def _wecom_event_label(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:80]
    return ""


def _split_wecom_text(text: str, max_bytes: int = 1800) -> list[str]:
    """Split output by UTF-8 bytes so mobile WeCom does not truncate CJK text."""
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for char in str(text or ""):
        char_bytes = len(char.encode("utf-8"))
        if current and current_bytes + char_bytes > max_bytes:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(char)
        current_bytes += char_bytes
    if current:
        chunks.append("".join(current))
    return chunks


def _load_wecom_progress_names(
    binding: ChannelBinding,
) -> tuple[dict[str, str], dict[str, dict[str, str]], dict[str, str]]:
    with Session(engine) as db:
        skill_names: dict[str, str] = {}
        step_names: dict[str, dict[str, str]] = {}
        for skill in db.exec(select(Skill).where(Skill.tenant_id == binding.tenant_id)).all():
            skill_names[skill.skill_id] = skill.name
            steps = {
                str(node.get("node_id") or "").strip(): str(node.get("name") or "").strip()
                for node in (skill.content_json or {}).get("nodes") or []
                if isinstance(node, dict)
                and str(node.get("node_id") or "").strip()
                and str(node.get("name") or "").strip()
            }
            if steps:
                step_names[skill.skill_id] = steps
        tool_names = {
            tool.name: str(tool.display_name or tool.description or "").strip()
            for tool in db.exec(select(Tool).where(Tool.tenant_id == binding.tenant_id)).all()
            if str(tool.display_name or tool.description or "").strip()
        }
        for skill in db.exec(
            select(GeneralSkill).where(GeneralSkill.tenant_id == binding.tenant_id)
        ).all():
            if skill.slug and skill.name:
                tool_names[f"general_skill.{skill.slug}"] = skill.name
    return skill_names, step_names, tool_names


async def _download_wecom_media_limited(url: str, aes_key: str) -> tuple[bytes, str | None]:
    from aibot import decrypt_file

    async with httpx.AsyncClient(timeout=15.0) as client, client.stream("GET", url) as response:
        response.raise_for_status()
        content_length = int(response.headers.get("content-length") or 0)
        ensure_channel_media_size(content_length, encrypted=True)
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_ENCRYPTED_CHANNEL_MEDIA_BYTES:
                raise ValueError(
                    f"企微附件超过大小上限: size>{MAX_ENCRYPTED_CHANNEL_MEDIA_BYTES}"
                )
            chunks.append(chunk)
        encrypted = b"".join(chunks)
        data = decrypt_file(encrypted, aes_key) if aes_key else encrypted
        if len(data) > MAX_CHANNEL_MEDIA_BYTES:
            raise ValueError(f"企微附件解密后超过大小上限: size={len(data)}")
        disposition = response.headers.get("content-disposition", "")
        match = re.search(r"filename\*=UTF-8''([^;\s]+)", disposition, re.IGNORECASE)
        if not match:
            match = re.search(r'filename="?([^";\s]+)', disposition, re.IGNORECASE)
        return data, unquote(match.group(1)) if match else None


def is_self_frame(frame: dict[str, Any]) -> bool:
    """机器人自身发送的消息回调(from.userid == aibotid)。"""
    body = frame.get("body") or {}
    sender = str((body.get("from") or {}).get("userid") or "").strip()
    bot_id = str(body.get("aibotid") or "").strip()
    return bool(sender) and bool(bot_id) and sender == bot_id


def _strip_bot_mention(text: str, *, is_group: bool) -> str:
    """Remove the leading WeCom bot mention before command parsing."""
    if not is_group:
        return text
    return re.sub(r"^\s*(?:<@[^>\s]+>|@[^\s]+)\s*", "", text).strip()


def normalize_wecom_frame(frame: dict[str, Any], *, account_scope: str = "") -> ChannelInbound | None:
    """归一化企微 WS 消息帧；自身消息/非文本语音/缺字段返回 None（丢弃）。"""
    if not isinstance(frame, dict) or is_self_frame(frame):
        return None
    body = frame.get("body") or {}
    msgtype = str(body.get("msgtype") or "")
    text = ""
    attachments: list[ChannelInboundAttachment] = []
    if msgtype == "text":
        text = str((body.get("text") or {}).get("content") or "").strip()
    elif msgtype == "voice":
        # 语音帧 body.voice.content 为微信侧转写文本
        text = str((body.get("voice") or {}).get("content") or "").strip()
    elif msgtype == "image":
        image = body.get("image") or {}
        media_url = str(image.get("url") or "").strip()
        media_id = str(image.get("media_id") or image.get("file_id") or "").strip()
        if media_url and validate_wecom_media_url(media_url):
            media_id = media_id or media_url
        if media_id:
            attachments.append(
                ChannelInboundAttachment(
                    media_id=media_id,
                    kind="image",
                    filename=f"{body.get('msgid') or 'image'}.jpg",
                    content_type="image/jpeg",
                    download_params={
                        "url": media_url,
                        "aes_key": str(image.get("aeskey") or "").strip(),
                    },
                )
            )
    elif msgtype == "file":
        file_info = body.get("file") or {}
        media_url = str(file_info.get("url") or "").strip()
        media_id = str(file_info.get("media_id") or file_info.get("file_id") or "").strip()
        if media_url and validate_wecom_media_url(media_url):
            media_id = media_id or media_url
        if media_id:
            attachments.append(
                ChannelInboundAttachment(
                    media_id=media_id,
                    kind="file",
                    filename=str(
                        file_info.get("file_name")
                        or file_info.get("filename")
                        or body.get("msgid")
                        or "attachment.bin"
                    ).strip(),
                    download_params={
                        "url": media_url,
                        "aes_key": str(file_info.get("aeskey") or "").strip(),
                    },
                )
            )
    elif msgtype == "mixed":
        mixed = body.get("mixed") or {}
        items = mixed.get("msg_item") or []
        if isinstance(items, list):
            text_parts: list[str] = []
            for index, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("msgtype") or "")
                if item_type == "text":
                    content = str((item.get("text") or {}).get("content") or "").strip()
                    if content:
                        text_parts.append(content)
                elif item_type in {"image", "file"}:
                    info = item.get(item_type) or {}
                    media_url = str(info.get("url") or "").strip()
                    if not media_url or not validate_wecom_media_url(media_url):
                        continue
                    filename = str(
                        info.get("file_name")
                        or info.get("filename")
                        or f"{body.get('msgid') or 'attachment'}-{index}"
                    ).strip()
                    if item_type == "image" and "." not in filename:
                        filename = f"{filename}.jpg"
                    attachments.append(
                        ChannelInboundAttachment(
                            media_id=media_url,
                            kind=item_type,
                            filename=filename,
                            content_type="image/jpeg" if item_type == "image" else "",
                            download_params={
                                "url": media_url,
                                "aes_key": str(info.get("aeskey") or "").strip(),
                            },
                        )
                    )
            text = "\n".join(text_parts)
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
    if text:
        text = _strip_bot_mention(text, is_group=is_group)
    if not text and not attachments:
        return None
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
        attachments=attachments,
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


class WeComStreamReply:
    """Render safe execution progress and model deltas into one stream message."""

    def __init__(
        self,
        binding: ChannelBinding,
        frame: dict[str, Any],
        stream: tuple[Any, Any],
        *,
        skill_names: dict[str, str] | None = None,
        step_names: dict[str, dict[str, str]] | None = None,
        tool_names: dict[str, str] | None = None,
    ):
        self._binding = binding
        self._frame = frame
        self._client, self._loop = stream
        base_stream_id = f"staffdeck:{binding.id}:{frame.get('body', {}).get('msgid', '')}"
        self._stream_id = f"{base_stream_id}:progress"
        self._answer_stream_id = f"{base_stream_id}:answer"
        body = frame.get("body") if isinstance(frame, dict) else None
        self._is_group = bool((body or {}).get("chatid"))
        self._condition = threading.Condition()
        self._progress: list[tuple[str, str]] = [("status", "📖 正在思考…")]
        self._answer = ""
        self._answer_started = False
        self._dirty = False
        self._finished = False
        self._failed = False
        self._animation_frame = 0
        # Mobile group chats can jump when a stream message is re-laid out.
        # Keep group progress event-driven and single-line; direct chats retain
        # the existing animated progress behavior.
        self._animation_enabled = not self._is_group
        self._processing_key: str | None = None
        self._last_progress_sent = ""
        self._last_answer_sent = ""
        self._skill_names = dict(skill_names or {})
        self._step_names = dict(step_names or {})
        self._tool_names = dict(tool_names or {})
        self._worker = threading.Thread(
            target=self._run,
            name=f"staffdeck-wecom-reply-{self._stream_id[-24:]}",
            daemon=True,
        )
        self._worker.start()
        with self._condition:
            self._dirty = True
            self._condition.notify_all()

    @property
    def failed(self) -> bool:
        with self._condition:
            return self._failed

    def on_delta(self, delta: str) -> None:
        if not delta:
            return
        with self._condition:
            if self._finished or self._failed:
                return
            self._answer += delta
            self._answer_started = True
            self._animation_enabled = False
            self._dirty = True
            self._condition.notify_all()

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Convert internal execution events to user-safe progress text."""
        progress = self._event_progress(event_type, payload)
        if not progress:
            return
        key, message = progress
        with self._condition:
            if self._finished or self._failed:
                return
            if self._is_group and self._answer_started:
                return
            self._animation_enabled = False
            if self._is_group:
                # Group-chat mobile clients are sensitive to changing message
                # height. Replace one status line instead of growing history.
                self._progress = [("status", message)]
                self._processing_key = None
                self._dirty = True
                self._condition.notify_all()
                return
            if key == "intent":
                self._progress = [item for item in self._progress if item[0] != "status"]
            if key in {"skill", "step", "task", "tool", "knowledge"} and message.startswith("⏳"):
                self._processing_key = key
                self._animation_enabled = True
            elif key in {"status", "intent"} or message.startswith(("✅", "❌", "📖")):
                self._processing_key = None
            self._upsert_progress(key, message)
            self._dirty = True
            self._condition.notify_all()

    def _event_progress(self, event_type: str, payload: dict[str, Any]) -> tuple[str, str] | None:
        """Render the same high-level milestones shown by the Feishu trace UI."""
        if event_type == "router_decision_created":
            intent = str(payload.get("user_intent") or payload.get("intent") or "").strip()
            return "intent", f"✅ 已判断意图：{intent}" if intent else "⏳ 判断意图…"
        if event_type in {"skill_started", "skill_resumed"}:
            skill_id = _wecom_event_label(payload, "to_skill_id", "from_skill_id", "skill_id")
            name = _wecom_event_label(payload, "skill_name", "skillName", "name")
            name = name or self._skill_names.get(skill_id, "")
            prefix = "进入流程" if event_type == "skill_started" else "恢复流程"
            return "skill", f"⏳ {prefix}：{name}…" if name else f"⏳ {prefix}…"
        if event_type == "skill_step_changed":
            skill_id = _wecom_event_label(payload, "to_skill_id", "from_skill_id", "skill_id")
            step_id = _wecom_event_label(payload, "to_step_id", "step_id")
            step = _wecom_event_label(payload, "to_step_name", "step_name")
            step = step or self._step_names.get(skill_id, {}).get(step_id, "")
            return "step", f"⏳ 推进流程：{step}…" if step else "⏳ 推进流程…"
        if event_type == "task_frame_started":
            skill_id = _wecom_event_label(payload, "skill_id")
            step_id = _wecom_event_label(payload, "step_id")
            name = _wecom_event_label(payload, "skill_name", "name", "title")
            name = self._step_names.get(skill_id, {}).get(step_id, "") or name
            name = name or self._skill_names.get(skill_id, "")
            return "task", f"⏳ 正在处理：{name}…" if name else "⏳ 正在处理当前任务…"
        if event_type in {"tool_call_started", "capability_started"}:
            name = _wecom_event_label(
                payload, "toolName", "tool_display_name", "display_name", "name", "toolId"
            )
            name = self._tool_names.get(name, name)
            return "tool", f"⏳ 调用工具：{name}…" if name else "⏳ 调用工具…"
        if event_type in {"tool_result", "tool_call_finished", "capability_completed"}:
            name = _wecom_event_label(
                payload, "toolName", "tool_display_name", "display_name", "tool_name", "name"
            )
            name = self._tool_names.get(name, name)
            failed = payload.get("isError") is True or payload.get("success") is False
            prefix = "❌ 能力调用失败" if failed else "✅ 能力调用完成"
            return "tool", f"{prefix}：{name}" if name else prefix
        if event_type in {"knowledge_query_started", "knowledge_search_started"}:
            query = payload.get("query")
            if isinstance(query, dict):
                query = query.get("query")
            query_text = str(query or payload.get("text") or "").strip()[:80]
            return "knowledge", f"⏳ 查询业务资料：{query_text}…" if query_text else "⏳ 查询业务资料…"
        if event_type in {"knowledge_query_finished", "knowledge_result", "knowledge_search_completed"}:
            chunks = payload.get("chunks")
            detail = f"，读取 {len(chunks)} 个资料片段" if isinstance(chunks, list) else ""
            return "knowledge", f"✅ 读取业务资料完成{detail}"
        fixed = {
            "handoff_requested": ("status", "⏳ 转接人工处理…"),
            "awaiting_user": ("status", "📖 流程已暂停，等待补充信息"),
            "skill_completed": ("skill", "✅ 完成流程"),
            "error_occurred": ("status", "❌ 流程未完成，正在整理结果…"),
        }
        return fixed.get(event_type)

    # Compatibility for focused callers/tests that only need the rendered text.
    def _event_message(self, event_type: str, payload: dict[str, Any]) -> str | None:
        progress = self._event_progress(event_type, payload)
        return progress[1] if progress else None

    def _upsert_progress(self, key: str, message: str) -> None:
        for index, (existing_key, _existing_message) in enumerate(self._progress):
            if existing_key == key:
                self._progress[index] = (key, message)
                return
        self._progress.append((key, message))
        self._progress = self._progress[-5:]

    def finish(self) -> bool:
        with self._condition:
            if not self._answer.strip():
                self._failed = True
                self._finished = True
                self._condition.notify_all()
                return False
            self._upsert_progress("status", "✅ 流程已结束")
            self._finished = True
            self._animation_enabled = False
            self._processing_key = None
            self._dirty = True
            self._condition.notify_all()
        self._worker.join(timeout=15.0)
        return not self.failed and not self._worker.is_alive()

    def abort(self) -> None:
        with self._condition:
            self._failed = True
            self._finished = True
            self._dirty = False
            self._condition.notify_all()
        self._worker.join(timeout=SEND_TIMEOUT_SECONDS)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._dirty and not self._finished:
                    self._condition.wait(timeout=WECOM_STREAM_MIN_INTERVAL_SECONDS)
                    if not self._dirty and not self._finished and self._animation_enabled:
                        self._animation_frame += 1
                        if self._processing_key:
                            for index, (key, message) in enumerate(self._progress):
                                if key == self._processing_key and message[:1] in WECOM_PROCESSING_FRAMES:
                                    self._progress[index] = (
                                        key,
                                        (
                                            f"{WECOM_PROCESSING_FRAMES[self._animation_frame % 2]}"
                                            f"{message[1:]}"
                                        ),
                                    )
                                    break
                        elif not self._answer:
                            self._upsert_progress(
                                "status",
                                f"{WECOM_PROGRESS_FRAMES[self._animation_frame % len(WECOM_PROGRESS_FRAMES)]} 正在思考…",
                            )
                        self._dirty = True
                if not self._dirty and not self._finished:
                    continue
                self._dirty = False
                finished = self._finished
                progress = self._render_progress_unlocked()
                answer = self._answer.strip()
            operations: list[tuple[str, str, bool]] = []
            if finished:
                operations.append((self._stream_id, progress, True))
                if answer:
                    operations.append((self._answer_stream_id, answer, True))
            else:
                if progress != self._last_progress_sent:
                    operations.append((self._stream_id, progress, False))
                if answer and answer != self._last_answer_sent:
                    operations.append((self._answer_stream_id, answer, False))
            for stream_id, content, stream_finished in operations:
                if not content:
                    continue
                if len(content.encode("utf-8")) > WECOM_STREAM_MAX_BYTES:
                    logger.warning(
                        "企微流式消息超过移动端安全长度，降级普通消息 binding=%s bytes=%s",
                        self._binding.id,
                        len(content.encode("utf-8")),
                    )
                    with self._condition:
                        self._failed = True
                    return
                if not stream_finished:
                    time.sleep(WECOM_STREAM_MIN_INTERVAL_SECONDS)
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._client.reply_stream(
                            self._frame,
                            stream_id,
                            content,
                            finish=stream_finished,
                        ),
                        self._loop,
                    )
                    future.result(timeout=SEND_TIMEOUT_SECONDS)
                    if stream_id == self._stream_id:
                        self._last_progress_sent = content
                    else:
                        self._last_answer_sent = content
                except Exception:
                    logger.exception("企微流式回复失败 binding=%s", self._binding.id)
                    with self._condition:
                        self._failed = True
                    return
            if finished:
                return

    def _render_content(self) -> str:
        with self._condition:
            return self._render_content_unlocked()

    def _render_progress(self) -> str:
        with self._condition:
            return self._render_progress_unlocked()

    def _render_progress_unlocked(self) -> str:
        return "\n\n".join(message for _key, message in self._progress)

    def _render_content_unlocked(self) -> str:
        progress = "\n\n".join(message for _key, message in self._progress)
        answer = self._answer.strip()
        return f"{progress}\n\n---\n\n{answer}" if answer else progress


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
        self._intake_engine = self._short_timeout_engine(self._engine)
        self._client_factory = client_factory or _default_client_factory
        self._reconcile_seconds = reconcile_seconds
        self._streams: dict[str, _StreamState] = {}
        self._paused: set[str] = set()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._reconcile_thread: threading.Thread | None = None

    @staticmethod
    def _short_timeout_engine(db_engine):
        """Use a separate short-timeout SQLite pool so inbox writes cannot stall heartbeats."""
        url = db_engine.url
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            return db_engine
        return create_engine(
            str(url),
            connect_args={"check_same_thread": False, "timeout": 0.25},
            poolclass=NullPool,
        )

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
            # 断开超时主动告警(一次性,重连后允许再次告警)
            self._maybe_alert_disconnect_timeout(row.id)

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
                statement = update(ChannelBinding).where(
                    ChannelBinding.id == binding_id,
                    ChannelBinding.channel == "wecom",
                )
                if config_revision is not None:
                    statement = statement.where(ChannelBinding.config_revision == config_revision)
                if connected:
                    statement = statement.where(ChannelBinding.status == "active")
                statement = statement.where(ChannelBinding.connected != connected)
                values: dict[str, Any] = {"connected": connected, "updated_at": utc_now()}
                if connected:
                    # 记录最近一次成功连接时间(断开超时告警的时间基准)
                    values["last_connected_at"] = utc_now()
                result = db.exec(statement.values(**values))
                if result.rowcount == 1:
                    if connected:
                        # 断开告警标记在重连成功时清除(允许下次再告警)
                        db.execute(
                            text(
                                "UPDATE channel_bindings "
                                "SET config_json = json_remove(config_json, '$.disconnect_alerted_at'), "
                                "updated_at = :updated_at "
                                "WHERE id = :binding_id"
                            ),
                            {"binding_id": binding_id, "updated_at": utc_now()},
                        )
                    db.commit()
                else:
                    db.rollback()
        except Exception:
            logger.exception("企微连接状态落库失败 binding=%s", binding_id)

    def _maybe_alert_disconnect_timeout(self, binding_id: str) -> None:
        """active 但持续未 connected 超阈值:给绑定创建者发一次性断开告警(config 标记防重复)。"""
        try:
            with Session(self._engine) as db:
                binding = db.get(ChannelBinding, binding_id)
                if not binding or binding.status != "active" or binding.connected:
                    return
                config = dict(binding.config_json or {})
                if config.get("disconnect_alerted_at"):
                    return
                # 时间基准:最近成功连接时间;从未连上过则以最近更新(保存凭证)时刻起算
                baseline = binding.last_connected_at or binding.updated_at
                if not baseline or utc_now() - baseline < timedelta(
                    minutes=WECOM_DISCONNECT_ALERT_MINUTES
                ):
                    return
                config["disconnect_alerted_at"] = utc_now().isoformat()
                binding.config_json = config
                binding.updated_at = utc_now()
                db.add(binding)
                db.commit()
                from app.channels.service_outbox import notify_binding_creator

                notify_binding_creator(
                    db,
                    binding,
                    "企业微信渠道长连接已断开超过 15 分钟，请在渠道接入页检查机器人配置或网络。",
                )
        except Exception:
            logger.exception("企微断开超时告警失败 binding=%s", binding_id)

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
            # SDK callback returns only after the durable inbox commit. The queue only wakes
            # the local worker; startup polling recovers the row if the process exits here.
            with state.callback_condition:
                if state.stop.is_set():
                    return
                state.callbacks_inflight += 1
            try:
                inbound = normalize_wecom_frame(frame, account_scope=account_scope)
                if inbound is None:
                    return
                from app.channels.service_durable_inbox import StageDisposition
                from app.channels.service_wecom_inbox import stage_wecom_inbound

                delay = 0.05
                while True:
                    result = stage_wecom_inbound(
                        db_engine=self._intake_engine,
                        binding_id=binding_id,
                        expected_revision=state.config_revision,
                        account_scope=account_scope,
                        inbound=inbound,
                    )
                    if result.disposition is not StageDisposition.NACK:
                        break
                    if self._stopped.is_set():
                        logger.error(
                            "企微服务停机时 inbox 仍不可写，终止当前回调 binding=%s event=%s",
                            binding_id,
                            inbound.event_id,
                        )
                        return
                    # The pinned SDK swallows listener exceptions and has no callback NACK.
                    # Keep the frame in this callback and apply backpressure until durable.
                    logger.warning(
                        "企微 inbox 暂不可写，保留当前消息并重试 binding=%s event=%s error=%s",
                        binding_id,
                        inbound.event_id,
                        result.error_code,
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 1.0)
                if result.disposition is StageDisposition.STAGED and result.event_pk:
                    state.queue.put_nowait(result.event_pk)
            except Exception:
                logger.exception("企微入站消息入队失败 binding=%s", binding_id)
                raise
            finally:
                with state.callback_condition:
                    state.callbacks_inflight -= 1
                    state.callback_condition.notify_all()

        client.on("authenticated", on_authenticated)
        client.on("disconnected", on_disconnected)
        client.on("message", on_frame)

    def _run_worker(self, binding_id: str, state: _StreamState) -> None:
        """单 worker 串行消费本绑定入站消息(与同会话串行锁语义一致)。"""
        from app.channels.service_intake import process_staged_inbound

        try:
            while True:
                # 强制退役:完成当前条后退出,不再接新消息
                if state.retired.is_set():
                    return
                item = state.queue.get()
                if item is None:
                    return
                try:
                    process_staged_inbound(item, db_engine=self._intake_engine)
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
                # Cleanup must not mask the worker's original failure, but it must be observable.
                logger.warning("企微事件循环关闭失败 binding=%s", binding_id, exc_info=True)


class WeComAdapter:
    """企微适配器:归一化 + 出站 send_message(run_coroutine_threadsafe)+ ingress。

    官方 SDK 无 typing 能力,故不实现 send_typing。
    """

    def normalize(self, raw: dict[str, Any]) -> ChannelInbound | None:
        return normalize_wecom_frame(raw)

    _token_provider: WeComTokenProvider | None = None

    def _get_token_provider(self) -> WeComTokenProvider:
        if self._token_provider is None:
            self._token_provider = WeComTokenProvider()
        return self._token_provider

    def download_media(
        self,
        binding: ChannelBinding,
        attachment: ChannelInboundAttachment,
        *,
        max_bytes: int = 0,
    ) -> bytes:
        media_url = str(attachment.download_params.get("url") or "").strip()
        if media_url:
            if not validate_wecom_media_url(media_url):
                raise ValueError("企微媒体 URL 域名不受信任")
            from app.channels import get_wecom_stream_manager

            stream = get_wecom_stream_manager().get_stream(binding.id)
            if not stream:
                raise RuntimeError(f"企微连接未就绪 binding={binding.id}")
            client, loop = stream
            future = asyncio.run_coroutine_threadsafe(
                _download_wecom_media_limited(
                    media_url,
                    str(attachment.download_params.get("aes_key") or "").strip(),
                ),
                loop,
            )
            data, downloaded_filename = future.result(timeout=15.0)
            if downloaded_filename:
                attachment.filename = downloaded_filename
            return data
        provider = self._get_token_provider()
        token = provider.get(binding)
        for attempt in range(2):
            try:
                with httpx.Client(timeout=15.0) as client, client.stream(
                    "GET",
                    f"{WECOM_API_BASE}/media/get",
                    params={"access_token": token, "media_id": attachment.media_id},
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if "application/json" not in content_type.lower():
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes(64 * 1024):
                            total += len(chunk)
                            if total > MAX_CHANNEL_MEDIA_BYTES:
                                raise ValueError("企微附件超过大小上限")
                            chunks.append(chunk)
                        return b"".join(chunks)
                    chunks = []
                    total = 0
                    for chunk in response.iter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > MAX_CHANNEL_MEDIA_BYTES:
                            raise ValueError("企微媒体响应超过大小上限")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
            except httpx.HTTPError as exc:
                raise WeComTokenError("企微媒体下载请求失败") from exc
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise WeComTokenError("企微 media/get 响应格式无效") from exc
            errcode = int(data.get("errcode") or 0)
            if errcode in {40014, 42001} and attempt == 0:
                token = provider.get(binding, force_refresh=True)
                continue
            raise WeComTokenError(
                f"企微 media/get 错误: errcode={errcode} msg={data.get('errmsg')}"
            )
        raise WeComTokenError("企微 media/get 下载失败")

    def send(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        chat_id = str(target.get("to_user_id") or "").strip()
        if not chat_id:
            raise ValueError("企微投递目标缺少 to_user_id(chatid)")
        # idempotency_key:企微 WS 协议的 req_id 由 SDK 内部生成,无客户端幂等标识可注入,仅保留签名一致
        from app.channels import get_wecom_stream_manager

        stream = get_wecom_stream_manager().get_stream(binding.id)
        if not stream:
            raise RuntimeError(f"企微连接未就绪 binding={binding.id}")
        client, loop = stream
        reply_to_user_id = str(target.get("reply_to_user_id") or "").strip()
        is_group = bool(target.get("is_group") and reply_to_user_id)
        quote = target.get("reply_quote") if isinstance(target.get("reply_quote"), dict) else {}
        quote_sender = str(quote.get("sender_name") or reply_to_user_id).strip()
        quote_text = str(quote.get("text") or "").strip().replace("\n", " ")
        quote_prefix = f"> {quote_sender}：{quote_text[:240]}\n\n" if is_group and quote_text else ""
        quote_prefix_bytes = len(quote_prefix.encode("utf-8"))
        content_limit = max(1, 1800 - quote_prefix_bytes)
        for chunk in _split_wecom_text(text, max_bytes=content_limit):
            if is_group:
                # WeCom does not expose an asynchronous native quote target.
                # Render the original sender and text as a Markdown quote.
                chunk = f"{quote_prefix}{chunk}"
            body = {"msgtype": "markdown", "markdown": {"content": chunk}}
            future = asyncio.run_coroutine_threadsafe(client.send_message(chat_id, body), loop)
            future.result(timeout=SEND_TIMEOUT_SECONDS)

    def create_stream_reply(
        self,
        binding: ChannelBinding,
        frame: dict[str, Any],
    ) -> WeComStreamReply | None:
        from app.channels import get_wecom_stream_manager

        stream = get_wecom_stream_manager().get_stream(binding.id)
        if not stream or not isinstance(frame, dict):
            return None
        skill_names, step_names, tool_names = _load_wecom_progress_names(binding)
        return WeComStreamReply(
            binding,
            frame,
            stream,
            skill_names=skill_names,
            step_names=step_names,
            tool_names=tool_names,
        )

    def start_ingress(self, binding_id: str) -> None:
        from app.channels import get_wecom_stream_manager

        get_wecom_stream_manager().ensure_binding(binding_id)

    def stop_ingress(self, binding_id: str) -> None:
        from app.channels import get_wecom_stream_manager

        get_wecom_stream_manager().stop_binding(binding_id)


class WeComTokenError(RuntimeError):
    pass


class WeComTokenProvider:
    """Cache enterprise access tokens for each binding and configuration revision."""

    def __init__(self, *, client_factory: Callable[[], httpx.Client] | None = None):
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=10.0))
        self._cache: dict[tuple[str, int], tuple[str, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(binding: ChannelBinding) -> tuple[str, int]:
        return binding.id, binding.config_revision

    def get(self, binding: ChannelBinding, *, force_refresh: bool = False) -> str:
        key = self._key(binding)
        with self._lock:
            cached = self._cache.get(key)
            if cached and not force_refresh and cached[1] > time.monotonic():
                return cached[0]
        config = dict(binding.config_json or {})
        corp_id = str(config.get("corp_id") or "").strip()
        if not corp_id or not binding.credentials_enc:
            raise WeComTokenError("企微绑定缺少 corp_id 或 secret")
        secret = decrypt_channel_secret(binding.credentials_enc)
        try:
            with self._client_factory() as client:
                response = client.get(
                    f"{WECOM_API_BASE}/gettoken",
                    params={"corpid": corp_id, "corpsecret": secret},
                )
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeComTokenError("企微 token 请求失败") from exc
        if response.status_code >= 400 or int(data.get("errcode", -1)) != 0:
            raise WeComTokenError(
                f"企微 token 请求失败: errcode={data.get('errcode')} msg={data.get('errmsg')}"
            )
        token = str(data.get("access_token") or "").strip()
        expires_in = int(data.get("expires_in") or 0)
        if not token or expires_in <= 0:
            raise WeComTokenError("企微 token 响应缺少必要字段")
        with self._lock:
            self._cache[key] = (
                token,
                time.monotonic() + max(1, expires_in - WECOM_TOKEN_REFRESH_SKEW_SECONDS),
            )
        return token


# 模块导入即注册企微适配器(渠道内核按注册表发现渠道)
register_channel_adapter("wecom", WeComAdapter())
