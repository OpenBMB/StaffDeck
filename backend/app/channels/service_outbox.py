from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import timedelta

from sqlalchemy import text
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.db.models import (
    ChannelBinding,
    ChannelBindingAgent,
    ChannelDelivery,
    ChannelIdentity,
    ChatSession,
    Message,
    new_id,
    utc_now,
)

logger = logging.getLogger(__name__)

_DELIVERY_BATCH_SIZE = 20
_SQLITE_SUPPORTS_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)
_delivery_thread: threading.Thread | None = None
_delivery_stop = threading.Event()


def _find_active_binding_for_agent(db: Session, chat_session: ChatSession) -> ChannelBinding | None:
    """仅为无 binding_id 的 legacy 会话按稳定账号键恢复 active binding。"""
    account_key = str(chat_session.channel_account_key or "").strip()
    if not account_key:
        return None
    candidates = db.exec(
        select(ChannelBinding)
        .where(
            ChannelBinding.tenant_id == chat_session.tenant_id,
            ChannelBinding.channel == chat_session.channel,
            ChannelBinding.status == "active",
            ChannelBinding.external_account_key == account_key,
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
        # 已锚定会话绝不跨 binding 回退，避免携带旧 target/context_token 串 Bot。
        binding = None
        binding_id = getattr(chat_session, "channel_binding_id", None)
        if binding_id:
            binding = db.get(ChannelBinding, binding_id)
            if not binding or binding.status != "active":
                return
            if binding.tenant_id != chat_session.tenant_id or binding.channel != chat_session.channel:
                return
            if (
                not chat_session.channel_account_key
                or chat_session.channel_account_key != binding.external_account_key
            ):
                return
        else:
            binding = _find_active_binding_for_agent(db, chat_session)
        if not binding:
            return
        if not binding_id:
            # 精确恢复成功后持久化归属，后续 staging/delivery 不再走 legacy 分支。
            conflicting_session = db.exec(
                select(ChatSession).where(
                    ChatSession.id != chat_session.id,
                    ChatSession.agent_id == chat_session.agent_id,
                    ChatSession.channel == chat_session.channel,
                    ChatSession.channel_binding_id == binding.id,
                    ChatSession.external_conv_id == chat_session.external_conv_id,
                )
            ).first()
            if conflicting_session:
                logger.warning(
                    "legacy 渠道会话认领冲突，跳过投递 session=%s existing=%s binding=%s",
                    chat_session.id,
                    conflicting_session.id,
                    binding.id,
                )
                return
            chat_session.channel_binding_id = binding.id
            db.add(chat_session)
            db.flush()
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


def _claim_due_deliveries(db: Session, limit: int = _DELIVERY_BATCH_SIZE) -> list[ChannelDelivery]:
    """单语句原子抢占 pending 投递:并发守护不会拿到同一行。

    SQLite 3.35+ 用 UPDATE ... RETURNING;旧版本走"UPDATE 后按 sending_since 回查"
    的等价路径(写入串行化下同样互斥)。
    """
    now = utc_now()
    if _SQLITE_SUPPORTS_RETURNING:
        rows = db.execute(
            text(
                """
                UPDATE channel_deliveries
                SET status = 'sending', sending_since = :now, updated_at = :now
                WHERE id IN (
                    SELECT id FROM channel_deliveries
                    WHERE status = 'pending' AND next_attempt_at <= :now
                    ORDER BY next_attempt_at
                    LIMIT :limit
                )
                RETURNING id
                """
            ),
            {"now": now, "limit": limit},
        ).all()
        db.commit()
        claimed_ids = [row[0] for row in rows]
    else:
        statement = text(
            """
            UPDATE channel_deliveries
            SET status = 'sending', sending_since = :now, updated_at = :now
            WHERE id IN (
                SELECT id FROM channel_deliveries
                WHERE status = 'pending' AND next_attempt_at <= :now
                ORDER BY next_attempt_at
                LIMIT :limit
            )
            """
        )
        db.execute(statement, {"now": now, "limit": limit})
        db.commit()
        claimed_ids = [
            row[0]
            for row in db.execute(
                text(
                    "SELECT id FROM channel_deliveries "
                    "WHERE status = 'sending' AND sending_since = :now"
                ),
                {"now": now},
            ).all()
        ]
    if not claimed_ids:
        return []
    return db.exec(
        select(ChannelDelivery)
        .where(ChannelDelivery.id.in_(claimed_ids))
        .order_by(ChannelDelivery.next_attempt_at)
    ).all()


def _deliver_due(db: Session) -> int:
    due = _claim_due_deliveries(db)
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
        delivery.sending_since = None
        delivery.updated_at = utc_now()
        db.add(delivery)
        db.commit()
        return
    if delivery.kind == "reply":
        chat_session = db.get(ChatSession, delivery.session_id)
        invalid_session = (
            not chat_session
            or chat_session.tenant_id != delivery.tenant_id
            or binding.tenant_id != delivery.tenant_id
            or chat_session.channel_binding_id != binding.id
            or chat_session.channel != binding.channel
            or not chat_session.channel_account_key
            or chat_session.channel_account_key != binding.external_account_key
        )
        if invalid_session:
            delivery.status = "failed"
            delivery.last_error = "渠道会话与绑定账号不一致"
            delivery.next_attempt_at = None
            delivery.sending_since = None
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
        adapter.send(
            binding,
            dict(delivery.target_json or {}),
            delivery.text,
            dedupe_key=delivery.idempotency_key,
        )
    except Exception as exc:
        delivery.last_error = str(exc)[:500]
        delivery.sending_since = None
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
    delivery.sending_since = None
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
        if _delivery_stop.wait(max(0.2, interval)):
            return


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


def stop_delivery_daemon(timeout_seconds: float = 5.0) -> bool:
    global _delivery_thread
    _delivery_stop.set()
    thread = _delivery_thread
    if thread and thread.is_alive():
        thread.join(timeout=max(0.0, timeout_seconds))
    stopped = not (thread and thread.is_alive())
    if stopped:
        _delivery_thread = None
    return stopped


def notify_binding_creator(db: Session, binding: ChannelBinding, text: str) -> None:
    """渠道异常主动告警:给绑定创建者发一条 kind=admin_alert 的渠道消息。

    创建者在该渠道已有身份(channel_identities 匹配)时才投递:优先取其最近私聊
    会话的 channel_target_json(含有效 context_token);无会话则按身份基本信息构造
    (微信侧缺 context_token 时投递会重试后失败,仅记日志可接受)。任何异常仅记日志。
    """
    try:
        if not binding.created_by_user_id:
            return
        identity = db.exec(
            select(ChannelIdentity).where(
                ChannelIdentity.tenant_id == binding.tenant_id,
                ChannelIdentity.channel == binding.channel,
                ChannelIdentity.staffdeck_user_id == binding.created_by_user_id,
            )
        ).first()
        if not identity:
            logger.info("渠道告警跳过:创建者在该渠道无身份 binding=%s", binding.id)
            return
        chat_session = db.exec(
            select(ChatSession)
            .where(
                ChatSession.tenant_id == binding.tenant_id,
                ChatSession.channel == binding.channel,
                ChatSession.user_id == binding.created_by_user_id,
                ChatSession.external_conv_id.is_not(None),
            )
            .order_by(ChatSession.updated_at.desc())
            .limit(1)
        ).first()
        if chat_session and (chat_session.channel_target_json or {}).get("to_user_id"):
            target = dict(chat_session.channel_target_json)
            session_id = chat_session.id
        else:
            target = {"to_user_id": identity.external_user_id, "context_token": ""}
            session_id = f"alert:{identity.id}"
        db.add(
            ChannelDelivery(
                tenant_id=binding.tenant_id,
                binding_id=binding.id,
                session_id=session_id,
                message_id=None,
                target_json=target,
                kind="admin_alert",
                text=text,
                status="pending",
                next_attempt_at=utc_now(),
                idempotency_key=new_id("chalert"),
            )
        )
        db.commit()
    except Exception:
        logger.exception("渠道告警投递登记失败 binding=%s", binding.id)
