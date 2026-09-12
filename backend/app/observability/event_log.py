from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlmodel import Session

from app.db.models import AgentEvent
from app.observability.spans import bind_span_sink

logger = logging.getLogger(__name__)


class EventLog:
    def __init__(
        self,
        db: Session,
        *,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.db = db
        self._event_sink = event_sink
        self._turn_id: str | None = None
        self._client_turn_id: str | None = None

    def bind_turn(self, turn_id: str, client_turn_id: str | None = None) -> None:
        self._turn_id = str(turn_id or "").strip() or None
        self._client_turn_id = str(client_turn_id or "").strip() or None

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict[str, Any]) -> AgentEvent:
        traced_payload = dict(payload)
        if self._turn_id:
            traced_payload.setdefault("turn_id", self._turn_id)
            traced_payload.setdefault("user_message_id", self._turn_id)
        if self._client_turn_id:
            traced_payload.setdefault("client_turn_id", self._client_turn_id)
        event = AgentEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=event_type,
            payload_json=traced_payload,
        )
        self.db.add(event)
        if self._event_sink is not None:
            try:
                self._event_sink(event_type, traced_payload)
            except Exception:
                logger.exception("event_sink 调用失败 event_type=%s", event_type)
        return event
_LLM_SPAN_METRIC_FIELDS = frozenset(
    {
        "span_id",
        "parent_span_id",
        "operation",
        "turn_id",
        "user_message_id",
        "client_turn_id",
        "task_frame_id",
        "iteration",
        "started_at",
        "finished_at",
        "duration_ms",
        "ttft_ms",
        "provider_setup_ms",
        "stream_duration_ms",
        "model",
        "model_name",
        "endpoint",
        "request_kind",
        "stream",
        "thinking_mode",
        "max_output_tokens",
        "response_mode",
        "request_parameters",
        "request_message_roles",
        "request_prefix_fingerprints",
        "provider_response_id",
        "attempt",
        "retry_count",
        "max_attempts",
        "json_attempt",
        "json_max_attempts",
        "json_retry_count",
        "context_message_count",
        "context_text_chars",
        "payload_chars",
        "request_message_chars",
        "request_message_count",
        "request_text_chars",
        "system_prompt_chars",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "uncached_input_tokens",
        "status",
        "finish_reason",
        "error_type",
        "error",
        "output_chars",
        "reasoning_chars",
        "stream_chunks",
    }
)
def _metrics_only(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """llm_call 事件只保留标量指标;其他 span 原样返回。

    裁剪过的行标记 bodies_omitted:审计视图对缺失的原文会渲染成空字符串与
    空数组,与"模型确实返回了空内容"无法区分。留一个显式标记,排障时才不会
    把"未留存"误读成"返回为空"。
    """
    if not event_type.startswith("llm_call_"):
        return payload
    kept = {key: value for key, value in payload.items() if key in _LLM_SPAN_METRIC_FIELDS}
    if len(kept) != len(payload):
        kept["bodies_omitted"] = True
    return kept
@contextmanager
def persist_spans(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
    client_turn_id: str | None = None,
) -> Iterator[None]:
    tenant_id = str(tenant_id or "").strip()
    session_id = str(session_id or "").strip()
    if not tenant_id or not session_id:
        yield
        return
    event_log = EventLog(db)
    if client_turn_id:
        event_log.bind_turn("", client_turn_id)
    def sink(event_type: str, payload: dict[str, Any]) -> None:
        event_log.record(tenant_id, session_id, event_type, _metrics_only(event_type, payload))
        db.commit()
    with bind_span_sink(sink):
        yield
