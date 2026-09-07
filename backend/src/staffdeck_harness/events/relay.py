"""SessionEventRelay and the ``event.observer`` slot.

The Harness v3 engine emits a rich ``session.event`` stream (``turn/start``, ``step/start``,
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


def _registry_observers() -> list[Any]:
    """Observers installed as ``event.observer`` modules (built-in and external)."""

    from staffdeck_harness.contracts.manifest import SlotName
    from staffdeck_harness.modules.registry import peek_registry

    reg = peek_registry()
    if reg is None:  # not built yet / being rebuilt: observers simply miss this event
        return []
    return [item.provider for item in reg.providers(SlotName.EVENT_OBSERVER) if callable(getattr(item.provider, "on_event", None))]


def _fanout(tenant_id: str, session_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
    with _lock:
        items: list[Any] = list(_observers.values())
    seen = {id(x) for x in items}
    items.extend(obs for obs in _registry_observers() if id(obs) not in seen)
    for obs in items:
        try:
            obs.on_event(tenant_id, session_id, event_type, payload)
        except Exception:  # observers never break the turn
            logger.exception("event observer %s failed on %s", getattr(obs, "name", getattr(obs, "module_id", obs)), event_type)


def fanout_event(tenant_id: str, session_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
    """Public entry for hosts that emit StaffDeck-side events (capability host, handoff core)."""

    _fanout(tenant_id, session_id, event_type, payload)


def relay_event(ev: Mapping[str, Any]) -> list[RuntimeEvent]:
    """Translate one Harness v3 session event into zero or more StaffDeck runtime events."""

    kind = str(ev.get("type") or "")
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    out: list[RuntimeEvent] = []
    if kind == "turn/start":
        out.append(("harness_v3_turn_started", {"turn": data.get("turn")}))
    elif kind == "step/start":
        out.append(("harness_v3_step_started", {"turn": data.get("turn"), "step": data.get("step")}))
    elif kind == "request/header":
        # The model call itself is observed by the bridge's model gateway (llm_call_started/finished
        # with tokens and duration); this only records what the engine asked for.
        out.append(("harness_v3_model_request", {"provider": data.get("provider"), "model": data.get("model"), "execution_engine": "harness_v3"}))
    elif kind == "assistant/chunk":
        # Engine shape (0.1.2): ``data.chunk = {type: text-delta | reasoning-delta | tool-call-delta, text?}``.
        # Only visible assistant text becomes a stream delta; reasoning and tool-call argument
        # deltas never reach the client. (``data.text`` is accepted for older/flat shapes.)
        chunk = data.get("chunk") if isinstance(data.get("chunk"), dict) else None
        text = None
        if chunk is not None:
            if chunk.get("type") == "text-delta" and isinstance(chunk.get("text"), str):
                text = chunk["text"]
        elif isinstance(data.get("text"), str):
            text = data["text"]
        if text:
            out.append(("stream_delta", {"content": text, "execution_engine": "harness_v3"}))
    elif kind == "tool/call":
        from staffdeck_harness.bridge.control import is_control_tool

        args = data.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                pass
        control = is_control_tool(str(data.get("name") or ""))
        out.append(("harness_action_created", {"action": "finish" if control else "tool", "tool_name": data.get("name"), "arguments": args, "call_id": data.get("callId"), "execution_engine": "harness_v3", **({"control": "submit_step_result"} if control else {})}))
    elif kind == "tool/result":
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool-result":
                out.append(("harness_tool_result", {"call_id": block.get("toolCallId"), "is_error": bool(block.get("isError")), "execution_engine": "harness_v3"}))
    elif kind == "assistant/message":
        out.append(("harness_v3_assistant_message", {"interrupted": bool(data.get("interrupted")), "execution_engine": "harness_v3"}))
    elif kind == "turn/end":
        out.append(("harness_v3_turn_ended", {"turn": data.get("turn"), "reason": data.get("reason") or data.get("finishReason")}))
    elif kind.startswith("hook/"):
        out.append(("harness_v3_hook_event", {"kind": kind, **{k: v for k, v in data.items() if k in {"event", "handlerId", "decision", "durationMs"}}}))
    elif kind.startswith("compaction/"):
        out.append(("harness_v3_compaction", {"kind": kind}))
    return out


class SessionEventRelay:
    """Bind to one StaffDeck turn: translate + persist via the engine's trace sink + fan out."""

    def __init__(self, tenant_id: str, session_id: str, trace: Callable[[str, dict[str, Any]], None] | None):
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.trace = trace
        self.count = 0
        self.control_calls: set[str] = set()

    def __call__(self, ev: Mapping[str, Any]) -> None:
        from staffdeck_harness.bridge.control import is_control_tool

        data = ev.get("data") or {}
        if ev.get("type") == "tool/call" and is_control_tool(str(data.get("name") or "")):
            self.control_calls.add(str(data.get("callId") or ""))
        for event_type, payload in relay_event(ev):
            if event_type == "harness_tool_result" and str(payload.get("call_id") or "") in self.control_calls:
                event_type = "harness_control_result"
            self.count += 1
            if self.trace:
                try:
                    self.trace(event_type, payload)
                except Exception:
                    logger.exception("trace sink failed for %s", event_type)
            _fanout(self.tenant_id, self.session_id, event_type, payload)
