from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.db.models import ChannelBinding, Skill

logger = logging.getLogger(__name__)

# 卡片更新最小间隔（秒）：规避飞书消息更新限流。
_MIN_UPDATE_INTERVAL = 1.0
# 单张卡片最多展示的步骤行数，超出截断尾部历史。
_MAX_LINES = 60


class _SinkEvent:
    """轻量 AgentEvent 替身，仅供 _event_trace_lines 渲染使用。

    EventLog.record 的 sink 收到的是 (event_type, payload_dict)，而
    _event_trace_lines 读取 event.event_type / event.payload_json / event.id /
    event.created_at 四个字段。这里用一个最小对象补齐，避免构造完整 ORM 行。
    """

    __slots__ = ("created_at", "event_type", "id", "payload_json")

    def __init__(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_type = event_type
        self.payload_json = payload
        self.id = str(payload.get("turn_id") or payload.get("user_message_id") or "")
        self.created_at = datetime.now(tz=UTC)


def _load_skill_names(db, tenant_id: str) -> dict[str, str]:
    from sqlmodel import select

    rows = db.exec(select(Skill).where(Skill.tenant_id == tenant_id)).all()
    return {row.skill_id: row.name for row in rows}


class FeishuTraceStreamer:
    """飞书渠道实时执行步骤卡片流式器。

    生命周期：
      start()  → 后台创建"正在执行"卡片，保存 message_id
      on_event → 累积 trace 行，节流后后台 PATCH 更新卡片
      finish() → 定格为完成状态，等待后台 worker 排空
      abort()  → 异常路径定格为失败状态

    所有 HTTP I/O 在后台 worker 线程执行，on_event 不阻塞调用方
    （即 AgentLoop 主线程）。start/finish/abort 同样非阻塞：
    finish/abort 仅入队最终状态任务后立即返回，不 join worker，
    避免飞书网络异常拖住会话锁。worker 为 daemon 线程，进程退出时
    自动结束；最终卡片更新由 worker 异步完成，失败仅记日志。

    全程 try/except 隔离：卡片创建/更新失败仅记日志，绝不抛出，不影响 turn
    成功与正文回复投递。
    """

    def __init__(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        turn_id: str,
        *,
        adapter: Any | None = None,
        skill_names: dict[str, str] | None = None,
        db=None,
        min_update_interval: float = _MIN_UPDATE_INTERVAL,
    ) -> None:
        self._binding = binding
        self._target = dict(target or {})
        self._turn_id = str(turn_id or "").strip()
        self._adapter = adapter
        self._skill_names = dict(skill_names or {})
        self._db = db
        self._min_update_interval = max(0.1, float(min_update_interval))
        self._message_id: str | None = None
        self._lines: list[dict] = []
        self._skill_hint: str | None = None
        self._lock = threading.Lock()
        self._last_update_at = 0.0
        self._dirty = False
        self._finished = False
        self._started = False
        self._final_state: str | None = None
        self._draining = False
        self._card_created = False

        # 后台 worker
        self._task_queue: queue.Queue[_Task | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_started = False

    # ---- 后台 worker ----

    def _start_worker(self) -> None:
        if self._worker_started:
            return
        self._worker_started = True
        self._worker = threading.Thread(
            target=self._worker_loop, name="feishu-trace-worker", daemon=True
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            try:
                task = self._task_queue.get(timeout=0.05)
            except queue.Empty:
                # finish/abort 已调用且队列排空：worker 可退出
                if self._draining and self._task_queue.empty():
                    return
                continue
            if task is None:
                return
            try:
                task.execute(self)
            except Exception:
                logger.exception(
                    "飞书 trace worker 任务执行失败 binding=%s turn=%s",
                    self._binding.id,
                    self._turn_id,
                )
            # 任务执行后再次检查：若已在 draining 且队列空，退出
            if self._draining and self._task_queue.empty():
                return

    def _stop_worker(self, *, timeout: float = 0.0) -> None:
        """标记 worker 进入 draining 状态。

        默认 timeout=0 表示不阻塞等待（用于 finish/abort 路径）：
        仅设置 _draining 标志，worker 在处理完已排队任务（含最终状态
        patch 及 _do_create_card 的补发任务）后自行退出。
        timeout>0 时阻塞 join 指定秒数（仅测试场景使用）。
        """
        if not self._worker_started or self._worker is None:
            return
        self._draining = True
        if timeout > 0:
            self._worker.join(timeout=timeout)
            self._worker_started = False
            self._worker = None

    # ---- adapter / skill names ----

    def _ensure_adapter(self):
        if self._adapter is not None:
            return self._adapter
        from app.channels.adapters.base import get_channel_adapter

        self._adapter = get_channel_adapter("feishu")
        return self._adapter

    def _ensure_skill_names(self) -> dict[str, str]:
        if self._skill_names or self._db is None:
            return self._skill_names
        try:
            self._skill_names = _load_skill_names(self._db, self._binding.tenant_id)
        except Exception:
            logger.exception("飞书 trace 流式器加载技能名称失败 tenant=%s", self._binding.tenant_id)
        return self._skill_names

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._start_worker()
        self._task_queue.put(_CreateCardTask())

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._finished:
            return
        try:
            self._ingest_event(event_type, payload)
            self._maybe_enqueue_patch()
        except Exception:
            logger.exception(
                "飞书 trace 事件处理失败 binding=%s turn=%s event=%s",
                self._binding.id,
                self._turn_id,
                event_type,
            )

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._final_state = "completed"
        with self._lock:
            for line in self._lines:
                if line.get("state") == "running":
                    line["state"] = "completed"
        if self._message_id:
            self._task_queue.put(_PatchCardTask(state="completed", force=True))
        # 不 join worker：避免飞书网络异常阻塞会话锁。
        # worker 为 daemon 线程，会处理完已排队任务（含最终状态 patch）后退出。
        # 卡片尚未创建时，_do_create_card 检测 _final_state 会补发最终状态。
        self._stop_worker()

    def abort(self, reason: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self._final_state = "failed"
        with self._lock:
            for line in self._lines:
                if line.get("state") == "running":
                    line["state"] = "failed"
        if self._message_id:
            self._task_queue.put(_PatchCardTask(state="failed", force=True))
        self._stop_worker()
        logger.info(
            "飞书 trace 流式器中止 binding=%s turn=%s reason=%s",
            self._binding.id,
            self._turn_id,
            reason,
        )

    # ---- 事件处理 ----

    def _ingest_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "router_decision_created":
            target_skill_id = str(payload.get("target_skill_id") or "").strip()
            if target_skill_id:
                self._skill_hint = target_skill_id

        from app.api.chat import _event_trace_lines

        sink_event = _SinkEvent(event_type, payload)
        lines = _event_trace_lines(sink_event, self._ensure_skill_names(), self._skill_hint)
        if not lines:
            skill_context = _skill_context_from_payload(event_type, payload, self._skill_hint)
            if skill_context:
                self._skill_hint = skill_context
            return
        with self._lock:
            for line in lines:
                _upsert_line(self._lines, line)
            if len(self._lines) > _MAX_LINES:
                self._lines = self._lines[-_MAX_LINES:]
            self._dirty = True

        skill_context = _skill_context_from_payload(event_type, payload, self._skill_hint)
        if skill_context:
            self._skill_hint = skill_context

    def _maybe_enqueue_patch(self) -> None:
        if not self._message_id:
            return
        with self._lock:
            if not self._dirty:
                return
            now = time.monotonic()
            if (now - self._last_update_at) < self._min_update_interval:
                return
            self._dirty = False
            self._last_update_at = now
            lines_snapshot = list(self._lines)
        self._task_queue.put(_PatchCardTask(lines=lines_snapshot, state="running", force=False))

    # ---- 卡片操作（在 worker 线程执行）----

    def _do_create_card(self) -> None:
        try:
            adapter = self._ensure_adapter()
            card = self._render_card(state="running")
            idempotency_key = f"feishu-trace:{self._binding.id}:{self._turn_id}"
            self._message_id = adapter.create_card(
                self._binding, self._target, card, idempotency_key=idempotency_key
            )
        except Exception:
            logger.exception(
                "飞书 trace 卡片创建失败 binding=%s turn=%s", self._binding.id, self._turn_id
            )
            self._message_id = None
        finally:
            self._card_created = True
        # 卡片创建成功后，处理累积行或最终状态
        if self._message_id:
            if self._final_state is not None:
                # finish/abort 已被调用，直接发送最终状态
                with self._lock:
                    lines_snapshot = list(self._lines)
                self._task_queue.put(
                    _PatchCardTask(lines=lines_snapshot, state=self._final_state, force=True)
                )
            else:
                with self._lock:
                    if self._dirty and self._lines:
                        self._dirty = False
                        self._last_update_at = time.monotonic()
                        lines_snapshot = list(self._lines)
                    else:
                        lines_snapshot = None
                if lines_snapshot is not None:
                    self._task_queue.put(
                        _PatchCardTask(lines=lines_snapshot, state="running", force=False)
                    )

    def _do_patch_card(self, lines: list[dict] | None, *, state: str, force: bool) -> None:
        if not self._message_id:
            return
        try:
            adapter = self._ensure_adapter()
            if lines is None:
                with self._lock:
                    lines = list(self._lines)
            card = self._render_card(lines=lines, state=state)
            adapter.update_card(self._binding, self._message_id, card)
        except Exception:
            logger.exception(
                "飞书 trace 卡片更新失败 binding=%s message_id=%s",
                self._binding.id,
                self._message_id,
            )

    # ---- 卡片渲染 ----

    def _render_card(
        self,
        *,
        lines: list[dict] | None = None,
        state: str = "running",
    ) -> dict[str, Any]:
        header_title = "正在思考…"
        header_template = "blue"
        if state == "completed":
            header_title = "执行完成"
            header_template = "green"
        elif state == "failed":
            header_title = "执行失败"
            header_template = "red"

        elements: list[dict[str, Any]] = []
        display_lines = lines if lines is not None else []
        for line in display_lines:
            elements.append(_line_to_card_element(line))
        if not display_lines:
            elements.append({"tag": "div", "text": {"tag": "plain_text", "content": "等待执行步骤…"}})

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": header_template,
            },
            "elements": elements,
        }


# ---- 后台任务 ----


class _Task:
    """worker 线程执行的抽象任务。"""

    def execute(self, streamer: FeishuTraceStreamer) -> None:
        raise NotImplementedError


class _CreateCardTask(_Task):
    def execute(self, streamer: FeishuTraceStreamer) -> None:
        streamer._do_create_card()


class _PatchCardTask(_Task):
    __slots__ = ("force", "lines", "state")

    def __init__(self, *, lines: list[dict] | None = None, state: str = "running", force: bool = False) -> None:
        self.lines = lines
        self.state = state
        self.force = force

    def execute(self, streamer: FeishuTraceStreamer) -> None:
        streamer._do_patch_card(self.lines, state=self.state, force=self.force)


def _line_to_card_element(line: dict) -> dict[str, Any]:
    text = str(line.get("text") or "").strip()
    detail = str(line.get("detail") or "").strip()
    state = str(line.get("state") or "").strip()
    icon = _state_icon(state)
    content_parts = [f"{icon} {text}" if icon else text]
    if detail:
        content_parts.append(detail)
    content = "\n".join(part for part in content_parts if part)
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _state_icon(state: str) -> str:
    if state == "completed":
        return "✅"
    if state == "failed":
        return "❌"
    if state == "running":
        return "⏳"
    return ""


def _upsert_line(lines: list[dict], line: dict) -> None:
    line_id = str(line.get("id") or "").strip()
    if line_id:
        for index, existing in enumerate(lines):
            if str(existing.get("id") or "") == line_id:
                lines[index] = {**existing, **line}
                return
    lines.append(line)


def _skill_context_from_payload(
    event_type: str,
    payload: dict[str, Any],
    skill_hint: str | None,
) -> str | None:
    if event_type in {"skill_started", "skill_resumed", "skill_step_changed"}:
        to_skill_id = str(payload.get("to_skill_id") or "").strip()
        from_skill_id = str(payload.get("from_skill_id") or "").strip()
        return to_skill_id or from_skill_id or skill_hint or None
    return None


def is_feishu_trace_enabled(binding: ChannelBinding | None) -> bool:
    if not binding or binding.channel != "feishu":
        return False
    if not get_settings().channel_feishu_trace_enabled:
        return False
    config = binding.config_json or {}
    return not (isinstance(config, dict) and config.get("trace_enabled") is False)
