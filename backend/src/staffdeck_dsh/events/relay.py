"""SessionEventRelay and the ``event.observer`` slot.

DSH emits a rich ``session.event`` stream (``turn/start``, ``step/start``,
``request/header``, ``assistant/chunk``, ``tool/call``, ``tool/result``,
``assistant/message``, ``turn/end`` ...). StaffDeck's trace/SSE/feedback
consumers understand the legacy ``AgentEvent`` vocabulary
(``harness_action_created``, ``harness_tool_result``, ``stream_delta`` ...).
The relay maps one to the other and fans the result out to registered
observers (Observability, Feedback, Evolution). Observers only *consume*; a
failing observer is logged and never affects the turn.

There is intentionally no persistent bus yet: the legacy ``EventLog`` writes
``AgentEvent`` rows synchronously and the SSE relay polls them. Observers that
need durability read those rows.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

OBSERVER_SLOT = "event.observer"

RuntimeEvent = tuple[str, dict[str, Any]]   # (event_type, payload)


@runtime_checkable
class EventObserver(Protocol):
    name: str

    def on_event(self, tenant_id: str, session_id: str, event_type: str, payload: Mapping[str, Any]) -> None: ...


_observers: dict[str, EventObserver] = {}
_lock = threading.Lock()


def register_observer(observer: EventObserver) -> None:
    with _lock:
        _observers[observer.name] = observer


def _fanout(tenant_id: str, session_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
    with _lock:
        items = list(_observers.values())
    for obs in items:
        try:
            obs.on_event(tenant_id, session_id, event_type, payload)
        except Exception:  # observers never break the turn
            logger.exception("event observer %s failed on %s", obs.name, event_type)


def relay_event(ev: Mapping[str, Any]) -> list[RuntimeEvent]:
    """Translate one DSH session event into zero or more StaffDeck runtime events."""

    kind = str(ev.get("type") or "")
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    out: list[RuntimeEvent] = []
    if kind == "turn/start":
        out.append(("dsh_turn_started", {"turn": data.get("turn")}))
    elif kind == "step/start":
        out.append(("dsh_step_started", {"turn": data.get("turn"), "step": data.get("step")}))
    elif kind == "request/header":
        out.append(("llm_call_started", {"provider": data.get("provider"), "model": data.get("model"), "engine": "dsh"}))
    elif kind == "assistant/chunk":
        text = data.get("text") if isinstance(data.get("text"), str) else None
        if text:
            out.append(("stream_delta", {"content": text, "execution_engine": "dsh"}))
    elif kind == "tool/call":
        args = data.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                pass
        out.append(("harness_action_created", {"action": "tool", "tool_name": data.get("name"), "arguments": args, "call_id": data.get("callId"), "execution_engine": "dsh"}))
    elif kind == "tool/result":
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool-result":
                out.append(("harness_tool_result", {"call_id": block.get("toolCallId"), "is_error": bool(block.get("isError")), "execution_engine": "dsh"}))
    elif kind == "assistant/message":
        out.append(("dsh_assistant_message", {"interrupted": bool(data.get("interrupted")), "execution_engine": "dsh"}))
    elif kind == "turn/end":
        out.append(("dsh_turn_ended", {"turn": data.get("turn"), "reason": data.get("reason") or data.get("finishReason")}))
    elif kind.startswith("hook/"):
        out.append(("dsh_hook_event", {"kind": kind, **{k: v for k, v in data.items() if k in {"event", "handlerId", "decision", "durationMs"}}}))
    elif kind.startswith("compaction/"):
        out.append(("dsh_compaction", {"kind": kind}))
    return out


class SessionEventRelay:
    """Bind to one StaffDeck turn: translate + persist via the engine's trace sink + fan out."""

    def __init__(self, tenant_id: str, session_id: str, trace: Callable[[str, dict[str, Any]], None] | None):
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.trace = trace
        self.count = 0

    def __call__(self, ev: Mapping[str, Any]) -> None:
        for event_type, payload in relay_event(ev):
            self.count += 1
            if self.trace:
                try:
                    self.trace(event_type, payload)
                except Exception:
                    logger.exception("trace sink failed for %s", event_type)
            _fanout(self.tenant_id, self.session_id, event_type, payload)
