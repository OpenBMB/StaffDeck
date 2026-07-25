from __future__ import annotations

import logging
import threading
from datetime import timedelta

from sqlalchemy import func, or_, update
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.db.models import (
    ChannelBinding,
    ChannelBindingAgent,
    ChannelDelivery,
    ChannelIdentity,
    ChannelInboundEvent,
    ChatSession,
    Message,
    new_id,
    utc_now,
)
from app.channels.service_identity import external_account_scope

logger = logging.getLogger(__name__)

_DELIVERY_BATCH_SIZE = 20
_REACTION_KINDS = {"reaction_add", "reaction_remove"}
# 启动重置卡死投递的阈值:仅重置 sending_since 为空或早于此秒数的 sending 行,
# 阈值内的视为仍被在飞 daemon 持有(避免交错启动重复投递)
SENDING_STALE_SECONDS = 120
_delivery_thread: threading.Thread | None = None
_reaction_delivery_thread: threading.Thread | None = None
_delivery_stop = threading.Event()
_FEISHU_DEDUP_RECOVERY_SECONDS = 55 * 60


def _stage_failed_delivery(
    db: Session,
    chat_session: ChatSession,
    message: Message,
    *,
    binding_id: str,
    target: dict,
    error: str,
) -> None:
    existing = db.exec(
        select(ChannelDelivery).where(ChannelDelivery.idempotency_key == message.id)
    ).first()
    if existing:
        return
    db.add(
        ChannelDelivery(
            tenant_id=chat_session.tenant_id,
            binding_id=binding_id,
            session_id=chat_session.id,
            message_id=message.id,
            target_json=dict(target),
            kind="reply",
            text=message.content,
            status="failed",
            next_attempt_at=None,
            last_error=error,
            idempotency_key=message.id,
        )
    )


def _immutable_delivery_target(
    db: Session,
    binding: ChannelBinding,
    chat_session: ChatSession,
    message: Message,
) -> dict:
    if binding.channel != "feishu":
        return dict(chat_session.channel_target_json or {})
    metadata = message.metadata_json or {}
    user_message_id = str(metadata.get("user_message_id") or "").strip()
    user_message = db.get(Message, user_message_id) if user_message_id else None
    client_turn_id = str(
        ((user_message.metadata_json or {}) if user_message else {}).get("client_turn_id")
        or metadata.get("client_turn_id")
        or ""
    ).strip()
    if not client_turn_id:
        return {}
    event = db.exec(
        select(ChannelInboundEvent).where(
            ChannelInboundEvent.binding_id == binding.id,
            ChannelInboundEvent.event_id == client_turn_id,
        )
    ).first()
    return dict(event.target_json or {}) if event else {}


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

    Web 会话不受渠道 staging 影响；渠道会话必须留下 delivery 或让事务失败。
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
                _stage_failed_delivery(
                    db,
                    chat_session,
                    message,
                    binding_id=binding_id,
                    target=dict(chat_session.channel_target_json or {}),
                    error="binding_missing_or_inactive",
                )
                return
            if binding.tenant_id != chat_session.tenant_id or binding.channel != chat_session.channel:
                raise RuntimeError("渠道会话与绑定租户或渠道不一致")
            if (
                not chat_session.channel_account_key
                or chat_session.channel_account_key != binding.external_account_key
            ):
                raise RuntimeError("渠道会话与绑定账号不一致")
        else:
            binding = _find_active_binding_for_agent(db, chat_session)
        if not binding:
            raise RuntimeError("渠道会话无法定位有效绑定")
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
                raise RuntimeError("legacy 渠道会话认领冲突")
            chat_session.channel_binding_id = binding.id
            db.add(chat_session)
            db.flush()
        target = _immutable_delivery_target(db, binding, chat_session, message)
        existing = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.idempotency_key == message.id)
        ).first()
        if existing:
            return
        if binding.channel == "feishu":
            valid_target = bool(target.get("message_id") or target.get("receive_id"))
        else:
            valid_target = bool(target.get("to_user_id") and target.get("context_token"))
        if not valid_target:
            _stage_failed_delivery(
                db,
                chat_session,
                message,
                binding_id=binding.id,
                target=target,
                error="delivery_target_missing",
            )
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
        if getattr(chat_session, "channel", None):
            raise


def _deliver_due(db: Session, *, reaction_lane: bool = False) -> int:
    now = utc_now()
    statement = (
        select(ChannelDelivery)
        .where(ChannelDelivery.status == "pending")
        .where(ChannelDelivery.next_attempt_at.is_not(None))
        .where(ChannelDelivery.next_attempt_at <= now)
        .order_by(ChannelDelivery.created_at)
        .limit(_DELIVERY_BATCH_SIZE)
    )
    if reaction_lane:
        statement = statement.where(ChannelDelivery.kind.in_(_REACTION_KINDS))
    else:
        statement = statement.where(ChannelDelivery.kind.notin_(_REACTION_KINDS))
    due_ids = db.exec(statement.with_only_columns(ChannelDelivery.id)).all()
    claimed = 0
    for delivery_id in due_ids:
        delivery = _claim_delivery(db, delivery_id, now=now, reaction_lane=reaction_lane)
        if delivery is None:
            continue
        claimed += 1
        _deliver_one(db, delivery)
    return claimed


def _claim_delivery(
    db: Session,
    delivery_id: str,
    *,
    now,
    reaction_lane: bool,
) -> ChannelDelivery | None:
    claim = (
        update(ChannelDelivery)
        .where(
            ChannelDelivery.id == delivery_id,
            ChannelDelivery.status == "pending",
            ChannelDelivery.next_attempt_at.is_not(None),
            ChannelDelivery.next_attempt_at <= now,
        )
        .values(
            status="sending",
            attempts=ChannelDelivery.attempts + 1,
            first_attempt_at=func.coalesce(ChannelDelivery.first_attempt_at, now),
            # 标记领取时刻:_reset_stuck_deliveries 据此区分在飞与卡死(120s 阈值)
            sending_since=now,
            updated_at=now,
        )
    )
    if reaction_lane:
        claim = claim.where(ChannelDelivery.kind.in_(_REACTION_KINDS))
    else:
        claim = claim.where(ChannelDelivery.kind.notin_(_REACTION_KINDS))
    result = db.exec(claim)
    db.commit()
    if result.rowcount != 1:
        return None
    return db.get(ChannelDelivery, delivery_id)


def _reaction_event_for_delivery(
    db: Session,
    delivery: ChannelDelivery,
) -> ChannelInboundEvent | None:
    target = dict(delivery.target_json or {})
    message_id = str(target.get("message_id") or "").strip()
    if not message_id:
        return None

    def valid(event: ChannelInboundEvent | None) -> bool:
        return bool(
            event
            and event.binding_id == delivery.binding_id
            and event.tenant_id == delivery.tenant_id
            and event.channel == "feishu"
            and event.event_id == message_id
        )

    event_pk = str(target.get("event_pk") or "").strip()
    event = db.get(ChannelInboundEvent, event_pk) if event_pk else None
    if event_pk:
        return event if valid(event) else None
    return db.exec(
        select(ChannelInboundEvent).where(
            ChannelInboundEvent.tenant_id == delivery.tenant_id,
            ChannelInboundEvent.binding_id == delivery.binding_id,
            ChannelInboundEvent.channel == "feishu",
            ChannelInboundEvent.event_id == message_id,
        )
    ).first()


def _stage_reaction_removal(
    db: Session,
    delivery: ChannelDelivery,
    event: ChannelInboundEvent,
) -> None:
    reaction_id = str(event.reaction_id or "").strip()
    if not reaction_id:
        return
    idempotency_key = f"feishu-reaction-remove:{event.id}:{reaction_id}"
    existing = db.exec(
        select(ChannelDelivery).where(ChannelDelivery.idempotency_key == idempotency_key)
    ).first()
    if existing:
        return
    db.add(
        ChannelDelivery(
            tenant_id=delivery.tenant_id,
            binding_id=delivery.binding_id,
            session_id=f"event:{event.id}",
            message_id=None,
            target_json={
                "message_id": event.event_id,
                "event_pk": event.id,
                "reaction_id": reaction_id,
            },
            kind="reaction_remove",
            text="",
            status="pending",
            next_attempt_at=utc_now(),
            idempotency_key=idempotency_key,
        )
    )


def _event_has_delivered_response(
    db: Session,
    event: ChannelInboundEvent,
) -> bool:
    deliveries = db.exec(
        select(ChannelDelivery).where(
            ChannelDelivery.binding_id == event.binding_id,
            ChannelDelivery.status == "delivered",
        )
    ).all()
    for row in deliveries:
        if row.kind in _REACTION_KINDS:
            continue
        target = row.target_json or {}
        is_final = row.kind in {"reply", "error_notice"} or bool(
            target.get("reaction_final")
        )
        if is_final and str(target.get("message_id") or "") == event.event_id:
            return True
    return False


def _deliver_one(db: Session, delivery: ChannelDelivery) -> None:
    from app.channels import binding_lifecycle_lock

    with binding_lifecycle_lock(delivery.binding_id):
        db.expire(delivery)
        db.refresh(delivery)
        if delivery.status != "sending":
            return
        _deliver_one_locked(db, delivery)


def _deliver_one_locked(db: Session, delivery: ChannelDelivery) -> None:
    from app.channels.adapters import get_channel_adapter

    settings = get_settings()
    binding = db.get(ChannelBinding, delivery.binding_id)
    allow_inactive_reaction = bool(
        binding
        and binding.channel == "feishu"
        and binding.credentials_enc
        and (
            delivery.kind == "reaction_remove"
            or (delivery.kind == "reaction_add" and delivery.attempts > 1)
        )
    )
    invalid_binding = (
        not binding
        or binding.tenant_id != delivery.tenant_id
        or (binding.status != "active" and not allow_inactive_reaction)
    )
    if invalid_binding:
        delivery.status = "failed"
        delivery.last_error = "渠道绑定不存在或已停用"
        delivery.sending_since = None
        delivery.updated_at = utc_now()
        db.add(delivery)
        db.commit()
        return
    reaction_event = None
    if delivery.kind in _REACTION_KINDS:
        if binding.channel != "feishu":
            delivery.status = "failed"
            delivery.last_error = "reaction 渠道无效"
            delivery.next_attempt_at = None
            delivery.updated_at = utc_now()
            db.add(delivery)
            db.commit()
            return
        reaction_event = _reaction_event_for_delivery(db, delivery)
        if not reaction_event:
            delivery.status = "failed"
            delivery.last_error = "reaction 事件边界无效"
            delivery.next_attempt_at = None
            delivery.updated_at = utc_now()
            db.add(delivery)
            db.commit()
            return
    if (
        binding.channel == "feishu"
        and delivery.kind not in _REACTION_KINDS
        and delivery.attempts > 0
        and delivery.first_attempt_at is not None
        and (utc_now() - delivery.first_attempt_at).total_seconds()
        > _FEISHU_DEDUP_RECOVERY_SECONDS
    ):
        delivery.status = "failed"
        delivery.last_error = "remote_state_unknown"
        delivery.next_attempt_at = None
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
    try:
        adapter = get_channel_adapter(binding.channel)
        target = dict(delivery.target_json or {})
        if delivery.kind == "reaction_add":
            add_reaction = getattr(adapter, "add_reaction", None)
            if not callable(add_reaction):
                raise RuntimeError("渠道适配器不支持 reaction")
            reaction_id = None
            if delivery.attempts > 1:
                find_reaction = getattr(adapter, "find_own_reaction", None)
                if not callable(find_reaction):
                    raise RuntimeError("渠道适配器不支持 reaction 恢复")
                reaction_id = find_reaction(
                    binding,
                    str(target.get("message_id") or ""),
                    delivery.text,
                )
            if not reaction_id and binding.status == "active":
                reaction_id = add_reaction(
                    binding,
                    str(target.get("message_id") or ""),
                    delivery.text,
                )
            if reaction_id:
                reaction_event.reaction_id = reaction_id
                reaction_event.updated_at = utc_now()
                db.add(reaction_event)
                if binding.status != "active" or _event_has_delivered_response(
                    db, reaction_event
                ):
                    _stage_reaction_removal(db, delivery, reaction_event)
        elif delivery.kind == "reaction_remove":
            remove_reaction = getattr(adapter, "remove_reaction", None)
            if not callable(remove_reaction):
                raise RuntimeError("渠道适配器不支持 reaction 清理")
            remove_reaction(
                binding,
                str(target.get("message_id") or ""),
                str(target.get("reaction_id") or ""),
            )
            if reaction_event.reaction_id == str(target.get("reaction_id") or ""):
                reaction_event.reaction_id = None
                reaction_event.updated_at = utc_now()
                db.add(reaction_event)
        else:
            adapter.send(
                binding,
                target,
                delivery.text,
                idempotency_key=delivery.idempotency_key,
            )
    except Exception as exc:
        delivery.last_error = str(exc)[:500]
        retryable = bool(getattr(exc, "retryable", True))
        if not retryable or delivery.attempts >= settings.channel_delivery_max_attempts:
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
    if binding.channel == "feishu" and delivery.kind not in _REACTION_KINDS:
        event = _reaction_event_for_delivery(db, delivery)
        target = delivery.target_json or {}
        is_final = delivery.kind in {"reply", "error_notice"} or bool(
            target.get("reaction_final")
        )
        if event and is_final:
            _stage_reaction_removal(db, delivery, event)
    db.commit()


def cleanup_feishu_reactions_before_binding_delete(
    db: Session,
    binding: ChannelBinding,
) -> None:
    """Delete known remote reactions before their binding credentials are removed."""
    if binding.channel != "feishu":
        return
    from app.channels.adapters import get_channel_adapter

    uncertain_adds = db.exec(
        select(ChannelDelivery).where(
            ChannelDelivery.tenant_id == binding.tenant_id,
            ChannelDelivery.binding_id == binding.id,
            ChannelDelivery.kind == "reaction_add",
            ChannelDelivery.attempts > 0,
            ChannelDelivery.status.in_({"pending", "sending", "failed"}),
        )
    ).all()
    adapter = get_channel_adapter("feishu")
    find_reaction = getattr(adapter, "find_own_reaction", None)
    remove_reaction = getattr(adapter, "remove_reaction", None)
    if not callable(find_reaction) or not callable(remove_reaction):
        raise RuntimeError("飞书适配器不支持 reaction 恢复与清理")
    event_by_id = {
        event.id: event
        for event in db.exec(
            select(ChannelInboundEvent).where(
                ChannelInboundEvent.tenant_id == binding.tenant_id,
                ChannelInboundEvent.binding_id == binding.id,
                ChannelInboundEvent.channel == "feishu",
            )
        ).all()
    }
    for delivery in uncertain_adds:
        event = event_by_id.get(str((delivery.target_json or {}).get("event_pk") or ""))
        if not event:
            raise RuntimeError("飞书 reaction 事件边界无效")
        if event.reaction_id:
            continue
        message_id = str((delivery.target_json or {}).get("message_id") or "").strip()
        if not message_id or message_id != event.event_id:
            raise RuntimeError("飞书 reaction 事件边界无效")
        reaction_id = find_reaction(binding, message_id, delivery.text)
        if reaction_id:
            remove_reaction(binding, message_id, reaction_id)
    events = db.exec(
        select(ChannelInboundEvent).where(
            ChannelInboundEvent.tenant_id == binding.tenant_id,
            ChannelInboundEvent.binding_id == binding.id,
            ChannelInboundEvent.channel == "feishu",
            ChannelInboundEvent.reaction_id.is_not(None),
        )
    ).all()
    for event in events:
        reaction_id = str(event.reaction_id or "").strip()
        if not reaction_id:
            continue
        remove_reaction(binding, event.event_id, reaction_id)
        event.reaction_id = None
        event.updated_at = utc_now()
        db.add(event)
    db.flush()


def _reset_stuck_deliveries(db: Session, *, reaction_lane: bool = False) -> None:
    now = utc_now()
    stale_before = now - timedelta(seconds=SENDING_STALE_SECONDS)
    statement = select(ChannelDelivery).where(
        ChannelDelivery.status == "sending",
        or_(
            ChannelDelivery.sending_since.is_(None),
            ChannelDelivery.sending_since <= stale_before,
        ),
    )
    if reaction_lane:
        statement = statement.where(ChannelDelivery.kind.in_(_REACTION_KINDS))
    else:
        statement = statement.where(ChannelDelivery.kind.notin_(_REACTION_KINDS))
    stuck = db.exec(statement).all()
    for row in stuck:
        binding = db.get(ChannelBinding, row.binding_id)
        if (
            binding
            and binding.channel == "feishu"
            and row.kind not in _REACTION_KINDS
            and row.first_attempt_at is not None
            and (now - row.first_attempt_at).total_seconds()
            > _FEISHU_DEDUP_RECOVERY_SECONDS
        ):
            row.status = "failed"
            row.last_error = "remote_state_unknown"
            row.next_attempt_at = None
            row.updated_at = now
            db.add(row)
            continue
        row.status = "pending"
        row.sending_since = None
        row.next_attempt_at = now
        row.updated_at = now
        db.add(row)
    if stuck:
        db.commit()


def run_delivery_daemon(
    *,
    once: bool = False,
    poll_seconds: float | None = None,
    db_engine=None,
) -> None:
    _run_delivery_lane(
        once=once,
        poll_seconds=poll_seconds,
        db_engine=db_engine,
        reaction_lane=False,
    )


def run_reaction_delivery_daemon(
    *,
    once: bool = False,
    poll_seconds: float | None = None,
    db_engine=None,
) -> None:
    _run_delivery_lane(
        once=once,
        poll_seconds=poll_seconds,
        db_engine=db_engine,
        reaction_lane=True,
    )


def _run_delivery_lane(
    *,
    once: bool,
    poll_seconds: float | None,
    db_engine,
    reaction_lane: bool,
) -> None:
    use_engine = db_engine or engine
    interval = poll_seconds if poll_seconds is not None else get_settings().channel_delivery_poll_seconds
    with Session(use_engine) as db:
        _reset_stuck_deliveries(db, reaction_lane=reaction_lane)
    while True:
        try:
            with Session(use_engine) as db:
                _deliver_due(db, reaction_lane=reaction_lane)
        except Exception:
            logger.exception("渠道投递守护轮询失败")
        if once or _delivery_stop.is_set():
            return
        if _delivery_stop.wait(max(0.2, interval)):
            return


def start_delivery_daemon(*, db_engine=None) -> None:
    global _delivery_thread, _reaction_delivery_thread
    _delivery_stop.clear()
    if not (_delivery_thread and _delivery_thread.is_alive()):
        _delivery_thread = threading.Thread(
            target=run_delivery_daemon,
            kwargs={"db_engine": db_engine},
            name="staffdeck-channel-delivery",
            daemon=True,
        )
        _delivery_thread.start()
    if not (_reaction_delivery_thread and _reaction_delivery_thread.is_alive()):
        _reaction_delivery_thread = threading.Thread(
            target=run_reaction_delivery_daemon,
            kwargs={"db_engine": db_engine},
            name="staffdeck-channel-reaction-delivery",
            daemon=True,
        )
        _reaction_delivery_thread.start()


def stop_delivery_daemon(timeout_seconds: float = 5.0) -> bool:
    global _delivery_thread, _reaction_delivery_thread
    _delivery_stop.set()
    threads = [_delivery_thread, _reaction_delivery_thread]
    deadline = utc_now() + timedelta(seconds=max(0.0, timeout_seconds))
    for thread in threads:
        if thread and thread.is_alive():
            remaining = max(0.0, (deadline - utc_now()).total_seconds())
            thread.join(timeout=remaining)
    stopped = not any(thread and thread.is_alive() for thread in threads)
    if stopped:
        _delivery_thread = None
        _reaction_delivery_thread = None
    return stopped


def notify_binding_creator(db: Session, binding: ChannelBinding, text: str) -> None:
    """渠道异常主动告警:给绑定创建者发一条 kind=admin_alert 的渠道消息。

    创建者在该渠道且与本 binding 同 scope(external_account_scope)下已有身份时才
    投递——多企业身份不跨 scope 发送;优先取其最近私聊会话的 channel_target_json
    (含有效 context_token);无会话则按身份基本信息构造(微信侧缺 context_token
    时投递会重试后失败,仅记日志可接受)。任何异常仅记日志。
    """
    try:
        if not binding.created_by_user_id:
            return
        scope = external_account_scope(db, binding)
        identity = db.exec(
            select(ChannelIdentity).where(
                ChannelIdentity.tenant_id == binding.tenant_id,
                ChannelIdentity.channel == binding.channel,
                ChannelIdentity.external_account_scope == scope,
                ChannelIdentity.staffdeck_user_id == binding.created_by_user_id,
            )
        ).first()
        if not identity:
            logger.info(
                "渠道告警跳过:创建者在该渠道同 scope 下无身份 binding=%s scope=%s",
                binding.id,
                scope,
            )
            return
        chat_session = db.exec(
            select(ChatSession)
            .where(
                ChatSession.tenant_id == binding.tenant_id,
                ChatSession.channel == binding.channel,
                # 限定本绑定:同一用户多账号时不得拿 A 账号的目标经 B 账号发送
                ChatSession.channel_binding_id == binding.id,
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
