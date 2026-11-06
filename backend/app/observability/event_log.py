from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlmodel import Session

from app.db.models import AgentEvent

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
        # Set by the engine that actually runs the turn ("harness_v3" when the Harness v3 bridge is active).
        # Legacy call sites stamp "harness_v2" literally; the override keeps every event of a turn consistent.
        self.execution_engine: str | None = None

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
        if self.execution_engine and traced_payload.get("execution_engine") in (None, "harness_v2"):
            traced_payload["execution_engine"] = self.execution_engine
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
