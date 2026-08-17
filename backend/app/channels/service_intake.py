from __future__ import annotations

import logging
import os
import threading
import time

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.channels.adapters.base import (
    ChannelInbound,
    channel_reaction_token,
    get_channel_adapter,
)
from app.channels.adapters.wechat import normalize_wechat_message
from app.channels.service_autoroute import maybe_auto_route, record_auto_route_event
from app.channels.service_durable_inbox import reaction_target
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
from app.config import get_settings
from app.db import engine
from app.db.models import (
    ChannelBindCode,
    ChannelBinding,
    ChannelDelivery,
    ChannelIdentity,
    ChannelInboundEvent,
    ChatSession,
    AgentEvent,
    MemoryRecord,
    Message,
    Team,
    User,
    new_id,
    utc_now,
)
from app.session.session_schema import ChatTurnRequest
from app.observability.spans import bind_span_sink

logger = logging.getLogger(__name__)

ERROR_NOTICE_TEXT = "处理出错，请稍后再试。"
INTERRUPTED_NOTICE_TEXT = "上一条消息处理中断，请重新发送。"
_DEDUP_LOOKBACK = 50
_processor_run_pid: int | None = None
_processor_run_id: str | None = None
_processor_run_guard = threading.Lock()
_staged_inbound_thread: threading.Thread | None = None
_staged_inbound_stop = threading.Event()
_staged_inbound_wake = threading.Event()
_binding_intake_condition = threading.Condition()
_binding_intake_active: dict[str, int] = {}
_binding_intake_paused: set[str] = set()


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

# /绑定 校验失败限流:按完整身份键 (tenant_id, channel, external_account_scope,
# external_user_id) 进程内计数(单进程够用,重启清零可接受)——其他租户/企业的失败
# 不影响本企业;连续 5 次失败冷却 10 分钟;1 小时无再失败淘汰,最多 10000 条
# (超出淘汰最旧),避免无界增长
_BIND_FAILURE_LIMIT = 5
_BIND_FAILURE_COOLDOWN_SECONDS = 600.0
_BIND_FAILURE_TTL_SECONDS = 3600.0
_BIND_FAILURE_MAX_ENTRIES = 10000
_BIND_COOLDOWN_TEXT = "尝试次数过多，请 10 分钟后再试。"
_bind_failures: dict[tuple[str, str, str, str], tuple[int, float]] = {}
_bind_failures_lock = threading.Lock()


def _prune_bind_failures(now: float) -> None:
    """淘汰 1 小时无再失败的条目;仍超硬上限则按最后失败时间淘汰最旧。

    调用方必须持有 _bind_failures_lock。
    """
    expired = [
        key
        for key, (_failures, since) in _bind_failures.items()
        if now - since > _BIND_FAILURE_TTL_SECONDS
    ]
    for key in expired:
        _bind_failures.pop(key, None)
    overflow = len(_bind_failures) - _BIND_FAILURE_MAX_ENTRIES
    if overflow > 0:
        oldest = sorted(_bind_failures, key=lambda key: _bind_failures[key][1])
        for key in oldest[:overflow]:
            _bind_failures.pop(key, None)


def _bind_cooldown_remaining(
    tenant_id: str, channel: str, account_scope: str, external_id: str
) -> float:
    """冷却剩余秒数;冷却结束自动清零重新计数。"""
    now = time.monotonic()
    with _bind_failures_lock:
        key = (tenant_id, channel, account_scope, external_id)
        failures, since = _bind_failures.get(key, (0, 0.0))
        if failures < _BIND_FAILURE_LIMIT:
            return 0.0
        remaining = _BIND_FAILURE_COOLDOWN_SECONDS - (now - since)
        if remaining > 0:
            return remaining
        _bind_failures.pop(key, None)
    return 0.0


def _record_bind_failure(tenant_id: str, channel: str, account_scope: str, external_id: str) -> None:
    now = time.monotonic()
    with _bind_failures_lock:
        key = (tenant_id, channel, account_scope, external_id)
        failures, _since = _bind_failures.get(key, (0, 0.0))
        _bind_failures[key] = (failures + 1, now)
        # 插入后淘汰:刚写入的 key 时间最新,不会被 TTL/上限误淘汰
        _prune_bind_failures(now)


def _reset_bind_failures(tenant_id: str, channel: str, account_scope: str, external_id: str) -> None:
    with _bind_failures_lock:
        _bind_failures.pop((tenant_id, channel, account_scope, external_id), None)


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


def claim_staged_inbound(event_id: str, *, db_engine=None) -> bool:
    """Atomically claim one durable received event for this processor generation."""
    use_engine = db_engine or engine
    with Session(use_engine) as db:
        result = db.exec(
            update(ChannelInboundEvent)
            .where(
                ChannelInboundEvent.id == event_id,
                ChannelInboundEvent.channel.in_({"feishu", "wecom", "dingtalk"}),
                ChannelInboundEvent.status == "received",
            )
            .values(
                status="processing",
                processor_run_id=current_processor_run_id(),
                updated_at=utc_now(),
            )
        )
        db.commit()
        return result.rowcount == 1


def pause_binding_intake(binding_id: str, timeout_seconds: float = 5.0) -> bool:
    """Fence new durable work and wait for an already-running turn to leave."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    with _binding_intake_condition:
        _binding_intake_paused.add(binding_id)
        while _binding_intake_active.get(binding_id, 0):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _binding_intake_condition.wait(timeout=remaining)
        return True


def resume_binding_intake(binding_id: str) -> None:
    with _binding_intake_condition:
        _binding_intake_paused.discard(binding_id)
        _binding_intake_condition.notify_all()


def _enter_binding_intake(binding_id: str) -> bool:
    with _binding_intake_condition:
        if binding_id in _binding_intake_paused:
            return False
        _binding_intake_active[binding_id] = _binding_intake_active.get(binding_id, 0) + 1
        return True


def _leave_binding_intake(binding_id: str) -> None:
    with _binding_intake_condition:
        remaining = _binding_intake_active.get(binding_id, 0) - 1
        if remaining > 0:
            _binding_intake_active[binding_id] = remaining
        else:
            _binding_intake_active.pop(binding_id, None)
        _binding_intake_condition.notify_all()


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
    if not _valid_notice_target(binding.channel, target):
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
    *,
    final_for_event: bool = False,
    idempotency_key: str | None = None,
) -> None:
    """系统提示投递(指令回复/员工下线提示);session_id 用 conv: 前缀占位。"""
    if not _valid_notice_target(binding.channel, target):
        return
    delivery_target = dict(target)
    if final_for_event:
        delivery_target["reaction_final"] = True
    db.add(
        ChannelDelivery(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            session_id=f"conv:{external_conv_id}",
            message_id=None,
            target_json=delivery_target,
            kind="notice",
            text=text,
            status="pending",
            next_attempt_at=utc_now(),
            idempotency_key=idempotency_key or new_id("chnotice"),
        )
    )


def _valid_notice_target(channel: str, target: dict) -> bool:
    if channel == "feishu":
        return bool(target.get("message_id") or target.get("receive_id"))
    return bool(target.get("to_user_id") and target.get("context_token"))


def _reaction_staging_enabled(channel: str) -> bool:
    """钉钉 emotion 常量与权限真机验证通过前默认不登记,避免堆积失败投递。"""
    if channel == "dingtalk":
        return get_settings().channel_dingtalk_reaction_enabled
    return True


def _stage_received_reaction(
    db: Session,
    binding: ChannelBinding,
    event: ChannelInboundEvent,
) -> None:
    """给已收到的入站事件登记"处理中"标记;渠道不支持 reaction 时直接跳过。"""
    token = channel_reaction_token(binding.channel)
    if not token or not _reaction_staging_enabled(binding.channel):
        return
    idempotency_key = f"{binding.channel}-reaction-add:{binding.id}:{event.event_id}"
    existing = db.exec(
        select(ChannelDelivery).where(ChannelDelivery.idempotency_key == idempotency_key)
    ).first()
    if existing:
        return
    db.add(
        ChannelDelivery(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            session_id=f"event:{event.id}",
            message_id=None,
            target_json=reaction_target(event),
            kind="reaction_add",
            text=token,
            status="pending",
            next_attempt_at=utc_now(),
            idempotency_key=idempotency_key,
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
    scope = external_account_scope(db, binding)
    # 限流:按完整身份键计数,连续错码/过期码达上限后冷却,冷却期内连正确码也拒绝
    if _bind_cooldown_remaining(binding.tenant_id, binding.channel, scope, external_id) > 0:
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
        _record_bind_failure(binding.tenant_id, binding.channel, scope, external_id)
        return "绑定码无效或已过期，请在 StaffDeck 网页端重新生成后再试。"
    owner = db.get(User, record.user_id)
    if not owner:
        _record_bind_failure(binding.tenant_id, binding.channel, scope, external_id)
        return "绑定码无效或已过期，请在 StaffDeck 网页端重新生成后再试。"

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
        _record_bind_failure(binding.tenant_id, binding.channel, scope, external_id)
        return "绑定码无效或已过期，请在 StaffDeck 网页端重新生成后再试。"
    _reset_bind_failures(binding.tenant_id, binding.channel, scope, external_id)

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
    label = channel_label(binding.channel)
    return f"绑定成功，{label}对话将与你的 StaffDeck 账号「{display}」共享记忆与对话记录。"


def _unbind_external_identity(
    db: Session,
    binding: ChannelBinding,
    inbound: ChannelInbound,
) -> str:
    scope = external_account_scope(db, binding)
    current = unbind_external_identity(
        db, binding.tenant_id, binding.channel, inbound.from_user_id, scope
    )
    label = channel_label(binding.channel)
    if not current:
        return f"当前{label}账号未绑定 StaffDeck 账号，无需解绑。"
    display = current.display_name or current.username
    return f"已解绑 StaffDeck 账号「{display}」，后续对话将使用独立的{label}访客身份。"


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


def process_inbound(
    binding: ChannelBinding,
    msg: dict | ChannelInbound,
    *,
    db_engine=None,
    staged_event_pk: str | None = None,
) -> bool:
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
        event = db.get(ChannelInboundEvent, staged_event_pk) if staged_event_pk else None
        if staged_event_pk:
            if (
                not event
                or event.binding_id != binding.id
                or event.event_id != inbound.event_id
                or event.status != "processing"
                or event.processor_run_id != current_processor_run_id()
            ):
                return False
            target = dict(event.target_json or {})
        else:
            event = ChannelInboundEvent(
                tenant_id=binding.tenant_id,
                binding_id=binding.id,
                channel=binding.channel,
                event_id=inbound.event_id,
                payload_json=inbound.raw,
                config_revision=binding.config_revision,
                target_json=target,
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
            elif binding.team_id:
                # 团队绑定:消息直路由团队 TL,员工列表/切换等指令无意义
                reply = "该渠道已接入团队，消息由团队 TL 统一接收，员工切换类指令不可用。"
            else:
                reply = run_command(db, binding, inbound.external_conv_id, command)
            _stage_notice(
                db,
                binding,
                inbound.external_conv_id,
                target,
                reply,
                final_for_event=True,
            )
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
        team: Team | None = None
        team_leader_agent_id: str | None = None
        if binding.team_id:
            # 团队绑定:跳过路由指针/自动分发,消息直路由团队现任 TL(换帅自动跟随)
            from app.teams.service import get_team_leader

            team_row = db.get(Team, binding.team_id)
            if team_row and team_row.tenant_id == binding.tenant_id and team_row.status == "active":
                team = team_row
            team_notice: str | None = None
            if team is None:
                team_notice = "该渠道绑定的团队已解散或停用，请联系管理员调整渠道绑定。"
            else:
                leader = get_team_leader(db, team.id)
                if leader is None:
                    team_notice = f"团队「{team.name}」暂未设置 TL，请先在 StaffDeck 网页端设置 TL 后再试。"
                else:
                    team_leader_agent_id = leader.agent_id
            if team_notice is not None:
                _stage_notice(
                    db,
                    binding,
                    inbound.external_conv_id,
                    target,
                    team_notice,
                    final_for_event=True,
                )
                event.status = "done"
                event.processed_at = utc_now()
                event.updated_at = utc_now()
                db.add(event)
                db.commit()
                return False
        if team is not None:
            current_agent_id = team_leader_agent_id
            pointer_reset = False
            route_decision = None
            chat_session = find_or_create_channel_session(
                db,
                binding,
                user,
                current_agent_id,
                inbound.external_conv_id,
                inbound.text,
                team_id=team.id,
                team_title=f"团队 {team.name} · TL 对话",
            )
        else:
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
        turn_team_id = team.id if team is not None else None

    with _session_lock(session_id):
        with Session(use_engine) as db:
            event = db.get(ChannelInboundEvent, event_id)
            chat_session = db.get(ChatSession, session_id)
            if not event or not chat_session:
                return False
            from app.core.agent_loop import AgentLoop

            attachments: list = []
            if inbound.attachments:
                from app.channels.attachment_bridge import inbound_attachments_to_chat

                try:
                    attachments = inbound_attachments_to_chat(
                        binding,
                        inbound,
                        db_engine=use_engine,
                        tenant_id=binding.tenant_id,
                        user_id=user_id,
                    )
                except Exception:
                    logger.exception(
                        "渠道附件下载失败 binding=%s event=%s",
                        binding.id,
                        inbound.event_id,
                    )

            # 团队绑定:重新挂到当前会话(跨 Session 边界只带 id)
            team = db.get(Team, turn_team_id) if turn_team_id else None
            user_message = _message_text(binding, inbound)
            context_injection = None
            interaction_mode = "normal"
            if team is not None:
                # 注入团队上下文(花名册/未闭环任务/黑板/派任务格式),与主聊天端 TL 会话同语义
                from app.teams.wakeup import build_tl_chat_context

                context_injection = build_tl_chat_context(db, team, user_message)
                interaction_mode = "team_tl"
            request = ChatTurnRequest(
                tenant_id=binding.tenant_id,
                session_id=session_id,
                agent_id=current_agent_id,
                user_id=user_id,
                message=user_message,
                context_injection=context_injection,
                channel=binding.channel,
                client_turn_id=inbound.event_id,
                attachments=attachments,
                interaction_mode=interaction_mode,
            )
            _send_wechat_typing(binding, inbound.from_user_id, inbound.context_token, 1, db_engine=use_engine)
            from app.channels.feishu_trace import FeishuTraceStreamer, is_feishu_trace_enabled

            trace_streamer: FeishuTraceStreamer | None = None
            if is_feishu_trace_enabled(binding):
                trace_streamer = FeishuTraceStreamer(
                    binding,
                    target,
                    inbound.event_id,
                    db=db,
                )
                trace_streamer.start()
            try:
                def persist_span(event_type: str, payload: dict[str, object]) -> None:
                    event_payload = dict(payload)
                    event_payload.setdefault("client_turn_id", inbound.event_id)
                    db.add(
                        AgentEvent(
                            tenant_id=binding.tenant_id,
                            session_id=session_id,
                            event_type=event_type,
                            payload_json=event_payload,
                        )
                    )
                    db.commit()

                with bind_span_sink(persist_span):
                    response = AgentLoop(
                        db, event_sink=trace_streamer.on_event if trace_streamer else None
                    ).handle_turn(request)
            except Exception as exc:
                logger.exception("渠道入站处理失败 binding=%s event=%s", binding.id, inbound.event_id)
                if trace_streamer:
                    trace_streamer.abort(str(exc)[:200])
                db.rollback()
                event = db.get(ChannelInboundEvent, event_id)
                chat_session = db.get(ChatSession, session_id)
                if not event or not chat_session:
                    return False
                event.status = "failed"
                event.error = str(exc)[:500]
                event.updated_at = utc_now()
                db.add(event)
                _stage_error_notice(db, binding, chat_session)
                db.commit()
                return False
            else:
                if trace_streamer:
                    trace_streamer.finish()
            finally:
                _send_wechat_typing(binding, inbound.from_user_id, inbound.context_token, 2, db_engine=use_engine)
            runtime_error_code = getattr(response, "runtime_error_code", None)
            response_reply = str(getattr(response, "reply", "") or "")
            if isinstance(response, dict):
                runtime_error_code = response.get("runtime_error_code")
                response_reply = str(response.get("reply") or "")
                if runtime_error_code is None and response_reply in {
                    "HARNESS_TURN_CONFLICT",
                    "HARNESS_SESSION_BUSY",
                }:
                    # Compatibility for older AgentLoop adapters that returned
                    # the exact conflict code in ``reply`` instead of the
                    # typed ``runtime_error_code`` field.
                    runtime_error_code = response_reply
            if runtime_error_code in {
                "HARNESS_TURN_CONFLICT",
                "HARNESS_SESSION_BUSY",
            }:
                # This is a retryable admission failure, not a completed
                # channel turn. Persist an explicit terminal notice for this
                # delivery (which also removes the processing reaction), then
                # release the inbox row for a later retry.
                event.status = "received"
                event.processor_run_id = None
                event.error = response_reply[:500]
                event.updated_at = utc_now()
                db.add(event)
                _stage_notice(
                    db,
                    binding,
                    str(inbound.external_conv_id or ""),
                    dict(event.target_json or {}),
                    response_reply,
                    final_for_event=True,
                    idempotency_key=f"channel-runtime-conflict:{binding.id}:{event.event_id}",
                )
                db.commit()
                return False
            event.status = "done"
            event.processed_at = utc_now()
            event.updated_at = utc_now()
            db.add(event)
            db.commit()
            if team is not None:
                # TL 回复后处理:解析派任务块并创建任务(与主聊天端同语义);
                # 后处理失败不影响本轮回复
                from app.teams.wakeup import process_tl_reply

                try:
                    turn_user = db.get(User, user_id)
                    if turn_user is not None:
                        process_tl_reply(
                            db,
                            team=team,
                            session=chat_session,
                            user=turn_user,
                            user_message=user_message,
                            reply=(response.reply if response else "") or "",
                            client_turn_id=inbound.event_id,
                        )
                except Exception:
                    logger.exception("渠道团队 TL 回复后处理失败 binding=%s", binding.id)
            return True


def process_staged_inbound(event_pk: str, *, db_engine=None) -> bool:
    """Claim and process a durable inbox row without re-registering the event."""
    use_engine = db_engine or engine
    with Session(use_engine) as db:
        staged = db.get(ChannelInboundEvent, event_pk)
        binding_id = staged.binding_id if staged else None
    if not binding_id or not _enter_binding_intake(binding_id):
        return False
    try:
        if not claim_staged_inbound(event_pk, db_engine=use_engine):
            return False
        return _process_claimed_staged_inbound(event_pk, db_engine=use_engine)
    except Exception:
        with Session(use_engine) as db:
            db.exec(
                update(ChannelInboundEvent)
                .where(
                    ChannelInboundEvent.id == event_pk,
                    ChannelInboundEvent.channel.in_({"feishu", "wecom", "dingtalk"}),
                    ChannelInboundEvent.status == "processing",
                    ChannelInboundEvent.processor_run_id == current_processor_run_id(),
                )
                .values(
                    status="received",
                    processor_run_id=None,
                    updated_at=utc_now(),
                )
            )
            db.commit()
        raise
    finally:
        _leave_binding_intake(binding_id)


def _process_claimed_staged_inbound(event_pk: str, *, db_engine=None) -> bool:
    use_engine = db_engine or engine
    with Session(use_engine) as db:
        event = db.get(ChannelInboundEvent, event_pk)
        binding = db.get(ChannelBinding, event.binding_id) if event else None
        if not event or not binding:
            if event:
                event.status = "failed"
                event.error = "binding_not_found"
                event.updated_at = utc_now()
                db.add(event)
                db.commit()
            return False
        try:
            inbound = _decode_and_validate_staged_event(event, binding)
        except ValueError as exc:
            event.status = "failed"
            event.error = str(exc)[:500]
            event.updated_at = utc_now()
            db.add(event)
            db.commit()
            return False
        _stage_received_reaction(db, binding, event)
        db.commit()
        db.refresh(binding)
        db.expunge(binding)
    try:
        return process_inbound(
            binding,
            inbound,
            db_engine=use_engine,
            staged_event_pk=event_pk,
        )
    except Exception:
        with Session(use_engine) as db:
            event = db.get(ChannelInboundEvent, event_pk)
            if event and event.status == "processing":
                turn_message = _find_turn_user_message_in_conv(
                    db, binding, inbound.external_conv_id, inbound.event_id
                )
                if not turn_message:
                    event.status = "received"
                    event.processor_run_id = None
                elif _turn_reply_exists(db, binding, turn_message):
                    event.status = "done"
                    event.error = None
                    event.processed_at = utc_now()
                else:
                    event.status = "failed"
                    event.error = "process_exit_incomplete_turn"
                    _stage_interrupted_notice(
                        db,
                        binding,
                        turn_message.session_id,
                        dict(event.target_json or {}),
                        inbound.event_id,
                    )
                event.updated_at = utc_now()
                db.add(event)
                db.commit()
        raise


def wake_staged_inbound_worker() -> None:
    _staged_inbound_wake.set()


def run_staged_inbound_daemon(
    *,
    once: bool = False,
    poll_seconds: float = 1.0,
    db_engine=None,
) -> None:
    use_engine = db_engine or engine
    while True:
        try:
            with Session(use_engine) as db:
                event_ids = db.exec(
                    select(ChannelInboundEvent.id)
                    .where(
                        ChannelInboundEvent.channel.in_({"feishu", "wecom", "dingtalk"}),
                        ChannelInboundEvent.status == "received",
                    )
                    .order_by(ChannelInboundEvent.created_at)
                    .limit(20)
                ).all()
            for event_id in event_ids:
                process_staged_inbound(event_id, db_engine=use_engine)
        except Exception:
            logger.exception("durable 入站事件轮询失败")
        if once or _staged_inbound_stop.is_set():
            return
        _staged_inbound_wake.wait(max(0.2, poll_seconds))
        _staged_inbound_wake.clear()


def start_staged_inbound_daemon(*, db_engine=None) -> None:
    global _staged_inbound_thread
    if _staged_inbound_thread and _staged_inbound_thread.is_alive():
        return
    _staged_inbound_stop.clear()
    _staged_inbound_wake.clear()
    _staged_inbound_thread = threading.Thread(
        target=run_staged_inbound_daemon,
        kwargs={"db_engine": db_engine},
        name="staffdeck-channel-durable-intake",
        daemon=True,
    )
    _staged_inbound_thread.start()


def stop_staged_inbound_daemon(timeout_seconds: float = 5.0) -> bool:
    global _staged_inbound_thread
    _staged_inbound_stop.set()
    _staged_inbound_wake.set()
    thread = _staged_inbound_thread
    if thread and thread.is_alive():
        thread.join(timeout=max(0.0, timeout_seconds))
    stopped = not (thread and thread.is_alive())
    if stopped:
        _staged_inbound_thread = None
    return stopped


def _decode_and_validate_staged_event(
    event: ChannelInboundEvent,
    binding: ChannelBinding,
) -> ChannelInbound:
    payload = event.payload_json or {}
    account = payload.get("account") if isinstance(payload, dict) else None
    if event.tenant_id != binding.tenant_id or event.channel != binding.channel:
        raise ValueError("replay_account_mismatch")
    if event.channel == "feishu":
        from app.channels.service_feishu_inbox import (
            decode_replay_envelope,
            feishu_account_key,
            feishu_identity_scope,
        )

        inbound = decode_replay_envelope(payload)
        app_id = str((account or {}).get("app_id") or "").strip()
        tenant_key = str((account or {}).get("tenant_key") or "").strip()
        if (
            not app_id
            or not tenant_key
            or binding.external_account_key != feishu_account_key(app_id)
            or binding.provider_tenant_key != tenant_key
            or binding.identity_scope_key != feishu_identity_scope(app_id, tenant_key)
        ):
            raise ValueError("replay_account_mismatch")
        return inbound
    if event.channel == "wecom":
        from app.channels.service_wecom_inbox import decode_wecom_replay_envelope

        inbound = decode_wecom_replay_envelope(payload)
        scope = str((account or {}).get("scope") or "").strip()
        if not scope or external_account_scope(None, binding) != scope:
            raise ValueError("replay_account_mismatch")
        return inbound
    if event.channel == "dingtalk":
        from app.channels.service_dingtalk_inbox import (
            decode_replay_envelope,
            dingtalk_account_key,
            dingtalk_identity_scope,
        )

        inbound = decode_replay_envelope(payload)
        client_id = str((account or {}).get("client_id") or "").strip()
        tenant_key = str((account or {}).get("tenant_key") or "").strip()
        if (
            not client_id
            or not tenant_key
            or binding.external_account_key != dingtalk_account_key(client_id)
            or binding.provider_tenant_key != tenant_key
            or binding.identity_scope_key != dingtalk_identity_scope(client_id, tenant_key)
        ):
            raise ValueError("replay_account_mismatch")
        return inbound
    raise ValueError("unsupported_envelope_channel")


def _recover_stale_durable_event(event_pk: str, *, db_engine=None) -> bool:
    use_engine = db_engine or engine
    run_id = current_processor_run_id()
    with Session(use_engine) as db:
        event = db.get(ChannelInboundEvent, event_pk)
        binding = db.get(ChannelBinding, event.binding_id) if event else None
        if not event or not binding or event.status != "processing":
            return False
        try:
            inbound = _decode_and_validate_staged_event(event, binding)
        except ValueError as exc:
            event.status = "failed"
            event.error = str(exc)[:500]
            event.updated_at = utc_now()
            db.add(event)
            db.commit()
            return False
        turn_message = _find_turn_user_message_in_conv(
            db, binding, inbound.external_conv_id, inbound.event_id
        )
        if turn_message:
            if not _claim_stale_event(db, event_pk):
                return False
            event = db.get(ChannelInboundEvent, event_pk)
            if _turn_reply_exists(db, binding, turn_message):
                event.status = "done"
                event.error = None
                event.processed_at = utc_now()
            else:
                event.status = "failed"
                event.error = "process_exit_incomplete_turn"
                _stage_interrupted_notice(
                    db,
                    binding,
                    turn_message.session_id,
                    dict(event.target_json or {}),
                    inbound.event_id,
                )
            event.updated_at = utc_now()
            db.add(event)
            db.commit()
            return False
        released = db.exec(
            update(ChannelInboundEvent)
            .where(
                ChannelInboundEvent.id == event_pk,
                ChannelInboundEvent.channel.in_({"feishu", "wecom", "dingtalk"}),
                ChannelInboundEvent.status == "processing",
                or_(
                    ChannelInboundEvent.processor_run_id.is_(None),
                    ChannelInboundEvent.processor_run_id != run_id,
                ),
            )
            .values(status="received", processor_run_id=None, updated_at=utc_now())
        )
        db.commit()
        if released.rowcount != 1:
            return False
    return process_staged_inbound(event_pk, db_engine=use_engine)


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
        candidates = [
            (row.id, row.binding_id, row.channel, row.event_id, row.payload_json)
            for row in stale_rows
        ]
    for event_pk, binding_id, channel, event_id, payload in candidates:
        with Session(use_engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            if not binding:
                continue
            if channel not in {"feishu", "wecom", "dingtalk"} and binding.status != "active":
                continue
            db.expunge(binding)
        try:
            if channel in {"feishu", "wecom", "dingtalk"}:
                if _recover_stale_durable_event(event_pk, db_engine=use_engine):
                    taken += 1
                continue
            inbound_payload = payload or {}
            if process_inbound(binding, inbound_payload, db_engine=use_engine):
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
