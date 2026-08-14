from __future__ import annotations

import logging
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
      start()  → 创建"正在执行"卡片，保存 message_id
      on_event → 累积 trace 行，节流 PATCH 更新卡片
      finish() → 定格为完成状态
      abort()  → 异常路径定格为失败状态

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

    def start(self) -> None:
        if self._started:
            return
        self._started = True
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

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._finished or not self._message_id:
            return
        try:
            self._ingest_event(event_type, payload)
            self._maybe_flush()
        except Exception:
            logger.exception(
                "飞书 trace 事件处理失败 binding=%s turn=%s event=%s",
                self._binding.id,
                self._turn_id,
                event_type,
            )

    def _ingest_event(self, event_type: str, payload: dict[str, Any]) -> None:
        # 维护 skill_hint（与 _build_turn_traces 同源逻辑）
        if event_type == "router_decision_created":
            target_skill_id = str(payload.get("target_skill_id") or "").strip()
            if target_skill_id:
                self._skill_hint = target_skill_id

        from app.api.chat import _event_trace_lines

        sink_event = _SinkEvent(event_type, payload)
        lines = _event_trace_lines(sink_event, self._ensure_skill_names(), self._skill_hint)
        if not lines:
            # 更新 skill_hint（部分事件携带 skill 上下文）
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

        # 更新 skill_hint
        skill_context = _skill_context_from_payload(event_type, payload, self._skill_hint)
        if skill_context:
            self._skill_hint = skill_context

    def _maybe_flush(self, *, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            now = time.monotonic()
            if not force and (now - self._last_update_at) < self._min_update_interval:
                return
            self._dirty = False
            self._last_update_at = now
            lines_snapshot = list(self._lines)
        self._patch_card(lines_snapshot, state="running")

    def _patch_card(self, lines: list[dict], *, state: str) -> None:
        if not self._message_id:
            return
        try:
            adapter = self._ensure_adapter()
            card = self._render_card(lines=lines, state=state)
            adapter.update_card(self._binding, self._message_id, card)
        except Exception:
            logger.exception(
                "飞书 trace 卡片更新失败 binding=%s message_id=%s",
                self._binding.id,
                self._message_id,
            )

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if not self._message_id:
            return
        with self._lock:
            # 把仍在 running 的行标记为 completed，定格展示
            for line in self._lines:
                if line.get("state") == "running":
                    line["state"] = "completed"
            lines_snapshot = list(self._lines)
        self._patch_card(lines_snapshot, state="completed")

    def abort(self, reason: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        if not self._message_id:
            return
        with self._lock:
            for line in self._lines:
                if line.get("state") == "running":
                    line["state"] = "failed"
            lines_snapshot = list(self._lines)
        self._patch_card(lines_snapshot, state="failed")
        logger.info(
            "飞书 trace 流式器中止 binding=%s turn=%s reason=%s",
            self._binding.id,
            self._turn_id,
            reason,
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
