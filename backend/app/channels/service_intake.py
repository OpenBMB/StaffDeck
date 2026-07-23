from __future__ import annotations

import logging
import os
import threading
import time

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.channels.adapters.base import ChannelInbound, get_channel_adapter
from app.channels.adapters.wechat import normalize_wechat_message
from app.channels.service_autoroute import maybe_auto_route, record_auto_route_event
from app.channels.service_identity import (
    channel_label,
    external_account_scope,
    external_identity_for_message,
    find_channel_identity,
    resolve_or_provision_user,
    unbind_external_identity,
)
from app.channels.service_routing import (
    ChannelCommand,
    agent_names,
    parse_command,
    resolve_current_agent,
    run_command,
)
from app.channels.service_session import find_or_create_channel_session
from app.db import engine
from app.db.models import (
    ChannelBindCode,
    ChannelBinding,
    ChannelDelivery,
    ChannelIdentity,
    ChannelInboundEvent,
    ChatSession,
    MemoryRecord,
    Message,
    User,
    new_id,
    utc_now,
)
from app.session.session_schema import ChatTurnRequest

logger = logging.getLogger(__name__)

ERROR_NOTICE_TEXT = "处理出错，请稍后再试。"
INTERRUPTED_NOTICE_TEXT = "上一条消息处理中断，请重新发送。"
_DEDUP_LOOKBACK = 50
_processor_run_pid: int | None = None
_processor_run_id: str | None = None
_processor_run_guard = threading.Lock()


def current_processor_run_id() -> str:
    global _processor_run_pid, _processor_run_id
    pid = os.getpid()
    if _processor_run_pid == pid and _processor_run_id:
        return _processor_run_id
    with _processor_run_guard:
        if _processor_run_pid != pid or not _processor_run_id:
            _processor_run_pid = pid
            _processor_run_id = new_id("chnrun")
        return _processor_run_id

# 进程级会话串行锁：同一渠道会话的入站消息顺序处理（拉模式天然有序）
_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()

# /绑定 校验失败限流:按 (channel, external_user_id) 进程内计数(单进程够用,
# 重启清零可接受)——连续 5 次失败冷却 10 分钟
_BIND_FAILURE_LIMIT = 5
_BIND_FAILURE_COOLDOWN_SECONDS = 600.0
_BIND_COOLDOWN_TEXT = "尝试次数过多，请 10 分钟后再试。"
_bind_failures: dict[tuple[str, str], tuple[int, float]] = {}
_bind_failures_lock = threading.Lock()


def _bind_cooldown_remaining(channel: str, external_id: str) -> float:
    """冷却剩余秒数;冷却结束自动清零重新计数。"""
    now = time.monotonic()
    with _bind_failures_lock:
        failures, since = _bind_failures.get((channel, external_id), (0, 0.0))
        if failures < _BIND_FAILURE_LIMIT:
            return 0.0
        remaining = _BIND_FAILURE_COOLDOWN_SECONDS - (now - since)
        if remaining > 0:
            return remaining
        _bind_failures.pop((channel, external_id), None)
    return 0.0


def _record_bind_failure(channel: str, external_id: str) -> None:
    with _bind_failures_lock:
        failures, _since = _bind_failures.get((channel, external_id), (0, 0.0))
        _bind_failures[(channel, external_id)] = (failures + 1, time.monotonic())


def _reset_bind_failures(channel: str, external_id: str) -> None:
    with _bind_failures_lock:
        _bind_failures.pop((channel, external_id), None)


def _claim_stale_event(db: Session, event_id: str) -> bool:
    """原子认领旧进程事件；当前进程持有的事件永不被墙钟接管。"""
    run_id = current_processor_run_id()
    result = db.exec(
        update(ChannelInboundEvent)
        .where(
            ChannelInboundEvent.id == event_id,
            ChannelInboundEvent.status == "processing",
            or_(
                ChannelInboundEvent.processor_run_id.is_(None),
                ChannelInboundEvent.processor_run_id != run_id,
            ),
        )
        .values(processor_run_id=run_id, updated_at=utc_now())
    )
    db.commit()
    return result.rowcount == 1


def _release_stale_event_claim(db_engine, event_id: str) -> None:
    """Release only this process's claim after recovery infrastructure fails."""
    run_id = current_processor_run_id()
    with Session(db_engine) as db:
        db.exec(
            update(ChannelInboundEvent)
            .where(
                ChannelInboundEvent.id == event_id,
                ChannelInboundEvent.status == "processing",
                ChannelInboundEvent.processor_run_id == run_id,
            )
            .values(processor_run_id=None, updated_at=utc_now())
        )
        db.commit()


def _release_stale_event_claim_by_key(
    db_engine,
    binding_id: str,
    external_event_id: str,
) -> None:
    """Release the currently surviving row after delete-and-recreate recovery fails."""
    run_id = current_processor_run_id()
    with Session(db_engine) as db:
        db.exec(
            update(ChannelInboundEvent)
            .where(
                ChannelInboundEvent.binding_id == binding_id,
                ChannelInboundEvent.event_id == external_event_id,
                ChannelInboundEvent.status == "processing",
                ChannelInboundEvent.processor_run_id == run_id,
            )
            .values(processor_run_id=None, updated_at=utc_now())
        )
        db.commit()


def _session_lock(session_id: str) -> threading.Lock:
    with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock


def _user_message_with_client_turn_exists(
    db: Session,
    session_id: str,
    client_turn_id: str,
    tenant_id: str,
) -> bool:
    rows = db.exec(
        select(Message)
        .where(
            Message.tenant_id == tenant_id,
            Message.session_id == session_id,
            Message.role == "user",
        )
        .order_by(Message.created_at.desc())
        .limit(_DEDUP_LOOKBACK)
    ).all()
    for row in rows:
        if str((row.metadata_json or {}).get("client_turn_id") or "") == client_turn_id:
            return True
    return False


def _find_turn_user_message_in_conv(
    db: Session,
    binding: ChannelBinding,
    external_conv_id: str,
    client_turn_id: str,
) -> Message | None:
    """在该 binding 的渠道会话(tenant 限定)内找此 client_turn_id 的用户消息。"""
    sessions = db.exec(
        select(ChatSession).where(
            ChatSession.tenant_id == binding.tenant_id,
            ChatSession.channel == binding.channel,
            ChatSession.channel_binding_id == binding.id,
        )
    ).all()
    legacy_prefixes = (
        "legacy_ambiguous_identity:",
        "legacy_cross_tenant:",
        "legacy_identity_mismatch:",
    )
    session_ids = []
    for chat_session in sessions:
        stored_conv = str(chat_session.external_conv_id or "")
        if stored_conv == external_conv_id:
            session_ids.append(chat_session.id)
            continue
        if stored_conv.startswith(legacy_prefixes):
            legacy_parts = stored_conv.split(":", 2)
            if len(legacy_parts) == 3 and legacy_parts[2] == external_conv_id:
                session_ids.append(chat_session.id)
    for session_id in session_ids:
        rows = db.exec(
            select(Message)
            .where(
                Message.tenant_id == binding.tenant_id,
                Message.session_id == session_id,
                Message.role == "user",
            )
            .order_by(Message.created_at.desc())
            .limit(_DEDUP_LOOKBACK)
        ).all()
        for row in rows:
            if str((row.metadata_json or {}).get("client_turn_id") or "") == client_turn_id:
                return row
    return None


def _client_turn_seen_in_conv(
    db: Session,
    binding: ChannelBinding,
    external_conv_id: str,
    client_turn_id: str,
) -> bool:
    """该外部会话(任意员工会话)是否已有此 client_turn_id 的用户消息(崩溃完成度判定)。"""
    return _find_turn_user_message_in_conv(db, binding, external_conv_id, client_turn_id) is not None


def _turn_reply_exists(db: Session, binding: ChannelBinding, user_message: Message) -> bool:
    """该用户消息对应的 assistant 回复是否已落库(turn_id/user_message_id 关联)。"""
    rows = db.exec(
        select(Message)
        .where(
            Message.tenant_id == binding.tenant_id,
            Message.session_id == user_message.session_id,
            Message.role == "assistant",
        )
        .order_by(Message.created_at)
    ).all()
    for row in rows:
        metadata = row.metadata_json or {}
        turn_ids = {
            str(metadata.get("turn_id") or "").strip(),
            str(metadata.get("user_message_id") or "").strip(),
            str(metadata.get("client_turn_id") or "").strip(),
        }
        if user_message.id in turn_ids:
            return True
    return False


def _stage_error_notice(db: Session, binding: ChannelBinding, chat_session: ChatSession) -> None:
    target = dict(chat_session.channel_target_json or {})
    if not target.get("to_user_id") or not target.get("context_token"):
        return
    db.add(
        ChannelDelivery(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            session_id=chat_session.id,
            message_id=None,
            target_json=target,
            kind="error_notice",
            text=ERROR_NOTICE_TEXT,
            status="pending",
            next_attempt_at=utc_now(),
            idempotency_key=new_id("chnotice"),
        )
    )


def _stage_interrupted_notice(
    db: Session,
    binding: ChannelBinding,
    session_id: str,
    target: dict,
    event_id: str,
) -> None:
    idempotency_key = f"channel-interrupted:{binding.id}:{event_id}"
    existing = db.exec(
        select(ChannelDelivery).where(ChannelDelivery.idempotency_key == idempotency_key)
    ).first()
    if existing:
        return
    db.add(
        ChannelDelivery(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            session_id=session_id,
            message_id=None,
            target_json=dict(target),
            kind="error_notice",
            text=INTERRUPTED_NOTICE_TEXT,
            status="pending",
            next_attempt_at=utc_now(),
            idempotency_key=idempotency_key,
        )
    )


def _stage_notice(
    db: Session,
    binding: ChannelBinding,
    external_conv_id: str,
    target: dict,
    text: str,
) -> None:
    """系统提示投递(指令回复/员工下线提示);session_id 用 conv: 前缀占位。"""
    if not target.get("to_user_id") or not target.get("context_token"):
        return
    db.add(
        ChannelDelivery(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            session_id=f"conv:{external_conv_id}",
            message_id=None,
            target_json=dict(target),
            kind="notice",
            text=text,
            status="pending",
            next_attempt_at=utc_now(),
            idempotency_key=new_id("chnotice"),
        )
    )


def _message_text(binding: ChannelBinding, inbound: ChannelInbound) -> str:
    if not inbound.is_group:
        return inbound.text
    sender_label = inbound.sender_name or external_identity_for_message(
        binding.channel,
        is_group=False,
        conv_key="",
        from_user_id=inbound.from_user_id,
    )[1]
    return f"[发送者: {sender_label}]\n{inbound.text}"


def _run_bind_command(
    db: Session,
    binding: ChannelBinding,
    inbound: ChannelInbound,
    cmd: ChannelCommand,
) -> str:
    """/绑定 <码> 与 /解绑:仅私聊生效,群聊提示不支持。"""
    if inbound.is_group:
        return "绑定/解绑只能在私聊中进行，群聊不支持该操作。"
    if cmd.kind == "bind":
        return _bind_external_identity(db, binding, inbound, cmd.query)
    return _unbind_external_identity(db, binding, inbound)


def _migrate_sessions(
    db: Session,
    binding: ChannelBinding,
    *,
    from_user_id: str,
    to_user: User,
    external_conv_id: str | None = None,
) -> set[str]:
    """把渠道会话迁到目标账号(群会话属于群账号,天然不受影响),返回迁移的 session_id 集。"""
    conditions = [ChatSession.user_id == from_user_id, ChatSession.channel == binding.channel]
    if external_conv_id is not None:
        conditions.append(ChatSession.external_conv_id == external_conv_id)
    sessions = db.exec(select(ChatSession).where(*conditions)).all()
    session_ids: set[str] = set()
    for row in sessions:
        row.user_id = to_user.id
        db.add(row)
        session_ids.add(row.id)
    return session_ids


def _migrate_memories(
    db: Session,
    *,
    from_user_id: str,
    to_user: User,
    session_ids: set[str] | None = None,
) -> None:
    """迁移记忆并同步 username;session_ids=None 表示整账号迁移(用于绑定)。"""
    conditions = [MemoryRecord.user_id == from_user_id]
    if session_ids is not None:
        if not session_ids:
            return
        conditions.append(MemoryRecord.session_id.in_(session_ids))
    memories = db.exec(select(MemoryRecord).where(*conditions)).all()
    for row in memories:
        row.user_id = to_user.id
        row.username = to_user.username
        db.add(row)


def _claim_bind_code(
    db: Session,
    binding: ChannelBinding,
    record: ChannelBindCode,
    submitted_code: str,
    now,
) -> bool:
    claim = db.exec(
        update(ChannelBindCode)
        .where(
            ChannelBindCode.tenant_id == binding.tenant_id,
            ChannelBindCode.id == record.id,
            ChannelBindCode.code == submitted_code,
            ChannelBindCode.used_at.is_(None),
            ChannelBindCode.expires_at > now,
        )
        .values(used_at=now)
    )
    return claim.rowcount == 1


def _bind_external_identity(
    db: Session,
    binding: ChannelBinding,
    inbound: ChannelInbound,
    code: str,
) -> str:
    code = (code or "").strip()
    if not code:
        return "用法：/绑定 <6位绑定码>。绑定码请在 StaffDeck 网页端生成。"
    external_id = inbound.from_user_id
    # 限流:连续错码/过期码达上限后冷却,冷却期内连正确码也拒绝
    if _bind_cooldown_remaining(binding.channel, external_id) > 0:
        return _BIND_COOLDOWN_TEXT
    now = utc_now()
    record = db.exec(
        select(ChannelBindCode)
        .where(
            ChannelBindCode.tenant_id == binding.tenant_id,
            ChannelBindCode.code == code,
        )
    ).first()
    if not record or record.used_at is not None or record.expires_at <= now:
        _record_bind_failure(binding.channel, external_id)
        return "绑定码无效或已过期，请在 StaffDeck 网页端重新生成后再试。"
    owner = db.get(User, record.user_id)
    if not owner:
        _record_bind_failure(binding.channel, external_id)
        return "绑定码无效或已过期，请在 StaffDeck 网页端重新生成后再试。"

    scope = external_account_scope(db, binding)
    identity = find_channel_identity(db, binding.tenant_id, binding.channel, external_id, scope)
    old_user_id = identity.staffdeck_user_id if identity else None
    if old_user_id and old_user_id != owner.id:
        current = db.get(User, old_user_id)
        if current and current.source == "web":
            display = current.display_name or current.username
            label = channel_label(binding.channel)
            return f"该{label}账号已绑定到 StaffDeck 账号「{display}」，请先发送 /解绑 解除后再绑定。"

    if not _claim_bind_code(db, binding, record, code, now):
        db.rollback()
        _record_bind_failure(binding.channel, external_id)
        return "绑定码无效或已过期，请在 StaffDeck 网页端重新生成后再试。"
    _reset_bind_failures(binding.channel, external_id)

    # ① 身份指针改指码主账号(无记录则新建)
    if identity:
        identity.staffdeck_user_id = owner.id
        identity.updated_at = utc_now()
    else:
        identity = ChannelIdentity(
            tenant_id=binding.tenant_id,
            channel=binding.channel,
            external_account_scope=scope,
            external_user_id=external_id,
            staffdeck_user_id=owner.id,
            display_name=owner.display_name,
        )
    db.add(identity)
    # ② 历史迁移:原懒建账号名下的渠道会话与全部记忆迁到码主账号
    if old_user_id and old_user_id != owner.id:
        _migrate_sessions(db, binding, from_user_id=old_user_id, to_user=owner)
        _migrate_memories(db, from_user_id=old_user_id, to_user=owner)
    display = owner.display_name or owner.username
    return f"绑定成功，微信对话将与你的 StaffDeck 账号「{display}」共享记忆与对话记录。"


def _unbind_external_identity(
    db: Session,
    binding: ChannelBinding,
    inbound: ChannelInbound,
) -> str:
    scope = external_account_scope(db, binding)
    current = unbind_external_identity(
        db, binding.tenant_id, binding.channel, inbound.from_user_id, scope
    )
    if not current:
        return "当前微信未绑定 StaffDeck 账号，无需解绑。"
    display = current.display_name or current.username
    return f"已解绑 StaffDeck 账号「{display}」，后续对话将使用独立的微信访客身份。"


def _send_wechat_typing(
    binding: ChannelBinding,
    ilink_user_id: str,
    context_token: str,
    status: int,
    *,
    db_engine=None,
    client_factory=None,
) -> None:
    """经适配器协议发送 typing(协议可选,无则跳过);保留原名与签名便于测试注入。"""
    try:
        adapter = get_channel_adapter(binding.channel)
        send_typing = getattr(adapter, "send_typing", None)
        if not callable(send_typing):
            return
        send_typing(
            binding,
            {"to_user_id": ilink_user_id, "context_token": context_token},
            status,
            db_engine=db_engine,
            client_factory=client_factory,
        )
    except Exception:
        logger.debug("渠道 typing 状态发送失败(忽略) binding=%s status=%s", binding.id, status, exc_info=True)


def _normalize_compat(binding: ChannelBinding, raw: dict) -> ChannelInbound | None:
    """原始帧兼容入口归一化(适配器入口侧应直接传 ChannelInbound)。"""
    if binding.channel == "wechat":
        config = dict(binding.config_json or {})
        return normalize_wechat_message(raw, ilink_bot_id=str(config.get("ilink_bot_id") or ""))
    if binding.channel == "wecom":
        from app.channels.adapters.wecom import normalize_wecom_frame

        return normalize_wecom_frame(raw, account_scope=external_account_scope(None, binding))
    adapter = get_channel_adapter(binding.channel)
    return adapter.normalize(raw)


def process_inbound(binding: ChannelBinding, msg: dict | ChannelInbound, *, db_engine=None) -> bool:
    """处理一条渠道入站消息：幂等登记 → 身份/会话锚定 → 串行执行对话轮。

    在 ingress 线程内同步调用；返回是否真正执行了对话轮。
    """
    use_engine = db_engine or engine
    if isinstance(msg, ChannelInbound):
        inbound = msg
    else:
        inbound = _normalize_compat(binding, msg)
    if inbound is None:
        return False
    # 作用域以绑定配置为准(适配器侧可能拿的是启动时旧值):统一覆盖后再使用
    scope = external_account_scope(None, binding)
    inbound.account_scope = scope
    command = parse_command(inbound.text)
    target = {
        "to_user_id": inbound.conv_key if inbound.is_group else inbound.from_user_id,
        "context_token": inbound.context_token,
    }

    with Session(use_engine) as db:
        event = ChannelInboundEvent(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            channel=binding.channel,
            event_id=inbound.event_id,
            payload_json=inbound.raw,
            status="processing",
            processor_run_id=current_processor_run_id(),
        )
        db.add(event)
        try:
            db.commit()
        except IntegrityError:
            # (binding_id, event_id) 唯一冲突:默认已处理跳过
            db.rollback()
            stale = db.exec(
                select(ChannelInboundEvent).where(
                    ChannelInboundEvent.binding_id == binding.id,
                    ChannelInboundEvent.event_id == inbound.event_id,
                )
            ).first()
            if (
                stale
                and stale.status == "processing"
                and _claim_stale_event(db, stale.id)
            ):
                claimed_id = stale.id
                try:
                    stale = db.get(ChannelInboundEvent, claimed_id)
                    turn_message = _find_turn_user_message_in_conv(
                        db, binding, inbound.external_conv_id, inbound.event_id
                    )
                    if not turn_message:
                        # 消息未落库(崩溃在登记后):删除旧行接管重跑
                        logger.warning(
                            "接管卡死的入站事件 binding=%s event=%s(status=%s,updated_at=%s)",
                            binding.id,
                            inbound.event_id,
                            stale.status,
                            stale.updated_at,
                        )
                        db.delete(stale)
                        db.commit()
                        return process_inbound(binding, inbound, db_engine=db_engine)
                    if not _turn_reply_exists(db, binding, turn_message):
                        # 消息已落库但 turn 未完成:不重跑(避免工具副作用重复),
                        # 标 failed + 向该会话发中断通知
                        logger.warning(
                            "入站事件 turn 未完成(崩溃窗口),标记失败 binding=%s event=%s",
                            binding.id,
                            inbound.event_id,
                        )
                        stale.status = "failed"
                        stale.error = "process_exit_incomplete_turn"
                        stale.updated_at = utc_now()
                        db.add(stale)
                        _stage_interrupted_notice(
                            db,
                            binding,
                            turn_message.session_id,
                            target,
                            inbound.event_id,
                        )
                        db.commit()
                    else:
                        stale.status = "done"
                        stale.error = None
                        stale.processed_at = utc_now()
                        stale.updated_at = utc_now()
                        db.add(stale)
                        db.commit()
                except Exception:
                    db.rollback()
                    _release_stale_event_claim_by_key(
                        use_engine,
                        binding.id,
                        inbound.event_id,
                    )
                    raise
            return False

        # 指令拦截:早于身份解析与会话创建,指令消息不进 AgentLoop
        if command:
            if command.kind in {"bind", "unbind"}:
                reply = _run_bind_command(db, binding, inbound, command)
            else:
                reply = run_command(db, binding, inbound.external_conv_id, command)
            _stage_notice(db, binding, inbound.external_conv_id, target, reply)
            event.status = "done"
            event.processed_at = utc_now()
            event.updated_at = utc_now()
            db.add(event)
            db.commit()
            return False

        external_id, display_name = external_identity_for_message(
            binding.channel,
            is_group=inbound.is_group,
            conv_key=inbound.conv_key,
            from_user_id=inbound.from_user_id,
            account_scope=scope,
        )
        user = resolve_or_provision_user(
            db, binding.tenant_id, binding.channel, external_id, display_name, scope
        )
        # 先在仍保持原 external_conv_id 的历史会话中去重。身份不一致会在后续
        # session 创建时隔离旧会话，若等隔离后再查会漏掉已落库的 turn。
        if _client_turn_seen_in_conv(
            db,
            binding,
            inbound.external_conv_id,
            inbound.event_id,
        ):
            event.status = "done"
            event.processed_at = utc_now()
            event.updated_at = utc_now()
            db.add(event)
            db.commit()
            return False
        current_agent_id, pointer_reset = resolve_current_agent(db, binding, inbound.external_conv_id)
        pre_route_agent_id = current_agent_id
        # 智能前台:LLM 意图分类自动分发(开关/挂载数/粘性保护由 maybe_auto_route 把关,异常全部回退当前)
        route_decision = maybe_auto_route(db, binding, current_agent_id, inbound.external_conv_id, inbound.text)
        if route_decision and route_decision.switched:
            current_agent_id = route_decision.agent_id
        chat_session = find_or_create_channel_session(
            db, binding, user, current_agent_id, inbound.external_conv_id, inbound.text
        )
        # 群聊回复投递到群会话，私聊投递到发言人
        chat_session.channel_target_json = target
        db.add(chat_session)
        if route_decision and route_decision.switched:
            names = agent_names(db, binding.tenant_id, [current_agent_id])
            routed_name = names.get(current_agent_id) or current_agent_id
            _stage_notice(
                db,
                binding,
                inbound.external_conv_id,
                target,
                f"已为你转接「{routed_name}」，输入 /员工 查看全部",
            )
        if route_decision:
            record_auto_route_event(db, binding, chat_session.id, route_decision, pre_route_agent_id)
        if pointer_reset:
            # 指针员工已下线,随本次回复前先补一条系统提示
            names = agent_names(db, binding.tenant_id, [current_agent_id])
            fallback_name = names.get(current_agent_id) or current_agent_id
            _stage_notice(
                db,
                binding,
                inbound.external_conv_id,
                target,
                f"当前员工已下线，已为你切回默认员工「{fallback_name}」。",
            )
        if _user_message_with_client_turn_exists(db, chat_session.id, inbound.event_id, binding.tenant_id):
            # 崩溃恢复去重：同一 event 的用户消息已落库
            event.status = "done"
            event.processed_at = utc_now()
            event.updated_at = utc_now()
            db.add(event)
            db.commit()
            return False
        db.commit()
        session_id = chat_session.id
        event_id = event.id
        user_id = user.id

    with _session_lock(session_id):
        with Session(use_engine) as db:
            event = db.get(ChannelInboundEvent, event_id)
            chat_session = db.get(ChatSession, session_id)
            if not event or not chat_session:
                return False
            from app.core.agent_loop import AgentLoop

            request = ChatTurnRequest(
                tenant_id=binding.tenant_id,
                session_id=session_id,
                agent_id=current_agent_id,
                user_id=user_id,
                message=_message_text(binding, inbound),
                channel=binding.channel,
                client_turn_id=inbound.event_id,
            )
            _send_wechat_typing(binding, inbound.from_user_id, inbound.context_token, 1, db_engine=use_engine)
            try:
                AgentLoop(db).handle_turn(request)
            except Exception as exc:
                logger.exception("渠道入站处理失败 binding=%s event=%s", binding.id, inbound.event_id)
                event.status = "failed"
                event.error = str(exc)[:500]
                event.updated_at = utc_now()
                db.add(event)
                _stage_error_notice(db, binding, chat_session)
                db.commit()
                return False
            finally:
                _send_wechat_typing(binding, inbound.from_user_id, inbound.context_token, 2, db_engine=use_engine)
            event.status = "done"
            event.processed_at = utc_now()
            event.updated_at = utc_now()
            db.add(event)
            db.commit()
            return True


def sweep_stale_inbound_events(*, db_engine=None) -> int:
    """启动恢复:接管其他进程代次遗留的 processing 事件,返回重跑数。

    事件 payload 即原始帧,交给 process_inbound(其内部陈旧接管逻辑会删旧行重走)。
    """
    use_engine = db_engine or engine
    run_id = current_processor_run_id()
    taken = 0
    with Session(use_engine) as db:
        stale_rows = db.exec(
            select(ChannelInboundEvent).where(
                ChannelInboundEvent.status == "processing",
                or_(
                    ChannelInboundEvent.processor_run_id.is_(None),
                    ChannelInboundEvent.processor_run_id != run_id,
                ),
            )
        ).all()
        candidates = [(row.binding_id, row.event_id, row.payload_json) for row in stale_rows]
    for binding_id, event_id, payload in candidates:
        with Session(use_engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            if not binding or binding.status != "active":
                continue
            db.expunge(binding)
        try:
            if process_inbound(binding, payload or {}, db_engine=use_engine):
                taken += 1
        except Exception:
            logger.exception("陈旧入站事件接管失败 binding=%s event=%s", binding_id, event_id)
            with Session(use_engine) as db:
                claimed = db.exec(
                    select(ChannelInboundEvent).where(
                        ChannelInboundEvent.binding_id == binding_id,
                        ChannelInboundEvent.event_id == event_id,
                    )
                ).first()
                claimed_id = claimed.id if claimed else None
            if claimed_id:
                _release_stale_event_claim(use_engine, claimed_id)
    if taken:
        logger.info("启动恢复:接管重跑 %s 个卡死入站事件", taken)
    return taken
