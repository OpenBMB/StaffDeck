from __future__ import annotations

import logging
import threading
from datetime import timedelta
from time import sleep

from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.db.models import (
    ChannelBinding,
    ChannelBindingAgent,
    ChannelDelivery,
    ChatSession,
    Message,
    utc_now,
)

logger = logging.getLogger(__name__)

_DELIVERY_BATCH_SIZE = 20
_delivery_thread: threading.Thread | None = None
_delivery_stop = threading.Event()


def _find_active_binding_for_agent(db: Session, chat_session: ChatSession) -> ChannelBinding | None:
    """挂载感知反查:active 且挂载集(含 agent_id 回退)包含会话员工的同渠道绑定。"""
    candidates = db.exec(
        select(ChannelBinding)
        .where(
            ChannelBinding.tenant_id == chat_session.tenant_id,
            ChannelBinding.channel == chat_session.channel,
            ChannelBinding.status == "active",
        )
        .order_by(ChannelBinding.created_at)
    ).all()
    if not candidates:
        return None
    binding_ids = [row.id for row in candidates]
    mount_rows = db.exec(
        select(ChannelBindingAgent).where(ChannelBindingAgent.binding_id.in_(binding_ids))
    ).all()
    mounts_by_binding: dict[str, set[str]] = {}
    for row in mount_rows:
        mounts_by_binding.setdefault(row.binding_id, set()).add(row.agent_id)
    for candidate in candidates:
        agent_ids = mounts_by_binding.get(candidate.id) or {candidate.agent_id}
        if chat_session.agent_id in agent_ids:
            return candidate
    return None


def stage_channel_delivery(db: Session, chat_session: ChatSession, message: Message) -> None:
    """把 assistant 回复登记为渠道 outbox 投递（随主事务提交，不单独 commit）。

    任何异常仅记日志——渠道 staging 绝不能弄挂 web 对话主链路。
    """
    try:
        if not getattr(chat_session, "channel", None):
            return
        # 优先按会话直挂的 channel_binding_id 直查;失败(绑定删除/停用或存量空值)回退挂载感知反查
        binding = None
        binding_id = getattr(chat_session, "channel_binding_id", None)
        if binding_id:
            binding = db.get(ChannelBinding, binding_id)
            if binding and binding.status != "active":
                binding = None
        if not binding:
            binding = _find_active_binding_for_agent(db, chat_session)
        if not binding:
            return
        target = dict(chat_session.channel_target_json or {})
        if not target.get("to_user_id") or not target.get("context_token"):
            return
        existing = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.idempotency_key == message.id)
        ).first()
        if existing:
            return
        db.add(
            ChannelDelivery(
                tenant_id=chat_session.tenant_id,
                binding_id=binding.id,
                session_id=chat_session.id,
                message_id=message.id,
                target_json=target,
                kind="reply",
                text=message.content,
                status="pending",
                next_attempt_at=utc_now(),
                idempotency_key=message.id,
            )
        )
    except Exception:
        logger.exception("渠道投递登记失败 session=%s", getattr(chat_session, "id", None))


def _deliver_due(db: Session) -> int:
    now = utc_now()
    due = db.exec(
        select(ChannelDelivery)
        .where(ChannelDelivery.status == "pending")
        .where(ChannelDelivery.next_attempt_at.is_not(None))
        .where(ChannelDelivery.next_attempt_at <= now)
        .order_by(ChannelDelivery.created_at)
        .limit(_DELIVERY_BATCH_SIZE)
    ).all()
    for delivery in due:
        _deliver_one(db, delivery)
    return len(due)


def _deliver_one(db: Session, delivery: ChannelDelivery) -> None:
    from app.channels.adapters import get_channel_adapter

    settings = get_settings()
    binding = db.get(ChannelBinding, delivery.binding_id)
    if not binding or binding.status != "active":
        delivery.status = "failed"
        delivery.last_error = "渠道绑定不存在或已停用"
        delivery.updated_at = utc_now()
        db.add(delivery)
        db.commit()
        return
    delivery.status = "sending"
    delivery.attempts += 1
    delivery.updated_at = utc_now()
    db.add(delivery)
    db.commit()
    try:
        adapter = get_channel_adapter(binding.channel)
        adapter.send(binding, dict(delivery.target_json or {}), delivery.text)
    except Exception as exc:
        delivery.last_error = str(exc)[:500]
        if delivery.attempts >= settings.channel_delivery_max_attempts:
            delivery.status = "failed"
            delivery.next_attempt_at = None
        else:
            delay = min(2**delivery.attempts, 300)
            delivery.status = "pending"
            delivery.next_attempt_at = utc_now() + timedelta(seconds=delay)
        delivery.updated_at = utc_now()
        db.add(delivery)
        db.commit()
        logger.warning("渠道投递失败(第 %s 次) delivery=%s: %s", delivery.attempts, delivery.id, exc)
        return
    delivery.status = "delivered"
    delivery.delivered_at = utc_now()
    delivery.last_error = None
    delivery.updated_at = utc_now()
    db.add(delivery)
    db.commit()


def _reset_stuck_deliveries(db: Session) -> None:
    stuck = db.exec(select(ChannelDelivery).where(ChannelDelivery.status == "sending")).all()
    for row in stuck:
        row.status = "pending"
        row.next_attempt_at = utc_now()
        row.updated_at = utc_now()
        db.add(row)
    if stuck:
        db.commit()


def run_delivery_daemon(
    *,
    once: bool = False,
    poll_seconds: float | None = None,
    db_engine=None,
) -> None:
    use_engine = db_engine or engine
    interval = poll_seconds if poll_seconds is not None else get_settings().channel_delivery_poll_seconds
    with Session(use_engine) as db:
        _reset_stuck_deliveries(db)
    while True:
        try:
            with Session(use_engine) as db:
                _deliver_due(db)
        except Exception:
            logger.exception("渠道投递守护轮询失败")
        if once or _delivery_stop.is_set():
            return
        sleep(max(0.2, interval))


def start_delivery_daemon(*, db_engine=None) -> None:
    global _delivery_thread
    if _delivery_thread and _delivery_thread.is_alive():
        return
    _delivery_stop.clear()
    _delivery_thread = threading.Thread(
        target=run_delivery_daemon,
        kwargs={"db_engine": db_engine},
        name="staffdeck-channel-delivery",
        daemon=True,
    )
    _delivery_thread.start()


def stop_delivery_daemon() -> None:
    _delivery_stop.set()
