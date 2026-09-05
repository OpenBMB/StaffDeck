"""Shared DSH session transport; contains no Staff/SOP/planning business policy."""

from __future__ import annotations
from typing import Any, Callable
from app.core.harness_agent import HarnessExecutionCancelled
from staffdeck_harness.events.relay import SessionEventRelay


def run_session(
    proc: Any,
    session_id: str,
    content_blocks: list[dict[str, Any]],
    cancelled: Callable[[], bool],
    trace: Callable[..., Any],
    *,
    tenant_id: str,
    host_session_id: str,
    timeout_seconds: float = 600.0,
) -> tuple[list[dict[str, Any]], str, str | None]:
    try:
        return _run_session(
            proc,
            session_id,
            content_blocks,
            cancelled,
            trace,
            tenant_id=tenant_id,
            host_session_id=host_session_id,
            timeout_seconds=timeout_seconds,
        )
    except BaseException:
        proc.close()  # a phase without an idle acknowledgement is never reusable
        raise


def _run_session(
    proc: Any,
    session_id: str,
    content_blocks: list[dict[str, Any]],
    cancelled: Callable[[], bool],
    trace: Callable[..., Any],
    *,
    tenant_id: str,
    host_session_id: str,
    timeout_seconds: float = 600.0,
) -> tuple[list[dict[str, Any]], str, str | None]:
    import time

    deadline = time.monotonic() + float(timeout_seconds or 600)
    client = proc.client
    events: list[dict[str, Any]] = []
    relay = SessionEventRelay(tenant_id, host_session_id, trace)
    with client.subscribe_session_notifications(session_id) as sub:
        message_id = client.session_prompt(
            session_id, content_blocks, notification_subscription=sub
        )
        received = False
        while True:
            if time.monotonic() >= deadline:
                proc.close()
                raise TimeoutError("engine phase did not become idle before its deadline")
            if cancelled():
                proc.close()  # this protocol has no cancellation; never reuse a busy worker
                raise HarnessExecutionCancelled("cancelled while Harness v3 turn running")
            # The Harness v3 (0.1.2) protocol has no cancel method and ``NotificationSubscription.next``
            # blocks indefinitely, so we poll with a small timeout: it lets us honour an
            # up-to-now-cancelled turn and break as soon as ``finish_task`` closed the slot
            # (a finished step is a closed step; the engine must not keep generating).
            n = next_notification(sub)
            if n is None:
                continue
            payload = n.payload or {}
            if n.method == "session.event" and payload.get("sessionId") == session_id:
                ev = payload.get("event")
                if isinstance(ev, dict):
                    if not received:
                        if ev.get("type") == "agent/inbox/spliced" and any(
                            isinstance(m, dict) and m.get("id") == message_id
                            for m in ((ev.get("data") or {}).get("inserted") or [])
                        ):
                            received = True
                        continue
                    events.append(ev)
                    relay(ev)
            if (
                n.method == "session.status"
                and payload.get("sessionId") == session_id
                and payload.get("status") == "idle"
                and received
            ):
                break
    from deepseek_harness.api import (
        final_response,
        finish_reason as _finish_reason,
    )  # official SDK helpers

    return events, final_response(events), _finish_reason(events)


def next_notification(sub: Any, *, timeout: float = 0.25) -> Any:
    """``NotificationSubscription.next()`` with a small timeout.

    Falls back to the blocking ``next()`` if the queue attribute the SDK uses is not present
    (e.g. a newer SDK changes its internals) — the feature degrades to no early-exit, never a
    crash.
    """

    q = getattr(sub, "_notifications", None)
    if q is None:
        return sub.next()
    try:
        return q.get(timeout=timeout)
    except Exception:
        return None
