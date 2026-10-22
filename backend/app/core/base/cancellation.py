from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import Literal

from sqlmodel import Session, select

from app.db.models import AgentEvent, HarnessTurnRecord

_lock = Lock()
_CANCEL_MARKER_TTL_SECONDS = 3_600.0
_MAX_CANCEL_MARKERS = 2_048
_cancelled_turns: OrderedDict[tuple[str, str], float] = OrderedDict()


def _prune_cancelled_turns_locked(now: float) -> None:
    cutoff = now - _CANCEL_MARKER_TTL_SECONDS
    while _cancelled_turns:
        _, created_at = next(iter(_cancelled_turns.items()))
        if created_at >= cutoff and len(_cancelled_turns) <= _MAX_CANCEL_MARKERS:
            break
        _cancelled_turns.popitem(last=False)


def cancel_chat_turn(session_id: str, turn_id: str) -> None:
    if not session_id or not turn_id:
        return
    with _lock:
        now = monotonic()
        key = (session_id, turn_id)
        _cancelled_turns.pop(key, None)
        _cancelled_turns[key] = now
        _prune_cancelled_turns_locked(now)


def clear_chat_turn_cancelled(session_id: str, turn_id: str) -> None:
    if not session_id or not turn_id:
        return
    with _lock:
        _cancelled_turns.pop((session_id, turn_id), None)


def is_chat_turn_cancelled(
    session_id: str,
    turn_id: str,
    *,
    db: Session | None = None,
    identity_kind: Literal["any", "client", "message"] = "any",
) -> bool:
    if not session_id or not turn_id:
        return False
    with _lock:
        _prune_cancelled_turns_locked(monotonic())
        if (session_id, turn_id) in _cancelled_turns:
            return True
    if db is None:
        return False

    receipt_query = select(HarnessTurnRecord).where(HarnessTurnRecord.session_id == session_id)
    if identity_kind == "message":
        receipt_query = receipt_query.where(HarnessTurnRecord.user_message_id == turn_id)
    elif identity_kind == "client":
        receipt_query = receipt_query.where(HarnessTurnRecord.client_turn_id == turn_id)
    else:
        receipt_query = receipt_query.where(
            (HarnessTurnRecord.client_turn_id == turn_id)
            | (HarnessTurnRecord.user_message_id == turn_id)
        )
    receipt = db.exec(receipt_query).first()
    if receipt is not None and receipt.status in {"completed", "failed", "cancelled"}:
        return receipt.status == "cancelled"

    # A cancellation may arrive before the user message or receipt exists.
    # The durable event is therefore the restart-safe source of truth until the
    # claimed turn records its own terminal status.
    rows = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.session_id == session_id,
            AgentEvent.event_type == "stream_cancelled",
        )
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
    ).all()
    for row in rows:
        payload = dict(row.payload_json or {})
        if identity_kind == "client":
            candidate_ids = {str(payload.get("client_turn_id") or "")}
        elif identity_kind == "message":
            candidate_ids = {
                str(payload.get("turn_id") or ""),
                str(payload.get("user_message_id") or ""),
                str(payload.get("message_id") or ""),
            }
        else:
            candidate_ids = {
                str(payload.get("turn_id") or ""),
                str(payload.get("user_message_id") or ""),
                str(payload.get("message_id") or ""),
                str(payload.get("client_turn_id") or ""),
            }
        if turn_id in candidate_ids:
            return True
    return False
