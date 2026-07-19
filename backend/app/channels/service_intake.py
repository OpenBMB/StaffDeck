from __future__ import annotations

import logging
import threading

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.channels.adapters.base import ChannelInbound, get_channel_adapter
from app.channels.adapters.wechat import normalize_wechat_message
from app.channels.service_autoroute import maybe_auto_route, record_auto_route_event
from app.channels.service_identity import (
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
_DEDUP_LOOKBACK = 50

# 进程级会话串行锁：同一渠道会话的入站消息顺序处理（拉模式天然有序）
_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock


def _user_message_with_client_turn_exists(db: Session, session_id: str, client_turn_id: str) -> bool:
    rows = db.exec(
        select(Message)
        .where(Message.session_id == session_id, Message.role == "user")
        .order_by(Message.created_at.desc())
        .limit(_DEDUP_LOOKBACK)
    ).all()
    for row in rows:
        if str((row.metadata_json or {}).get("client_turn_id") or "") == client_turn_id:
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


def _bind_external_identity(
    db: Session,
    binding: ChannelBinding,
    inbound: ChannelInbound,
    code: str,
) -> str:
    code = (code or "").strip()
    if not code:
        return "用法：/绑定 <6位绑定码>。绑定码请在 StaffDeck 网页端生成。"
    now = utc_now()
    record = db.exec(
        select(ChannelBindCode)
        .where(
            ChannelBindCode.tenant_id == binding.tenant_id,
            ChannelBindCode.code == code,
        )
        .order_by(ChannelBindCode.created_at.desc())
    ).first()
    if not record or record.used_at is not None or record.expires_at <= now:
        return "绑定码无效或已过期，请在 StaffDeck 网页端重新生成后再试。"
    owner = db.get(User, record.user_id)
    if not owner:
        return "绑定码无效或已过期，请在 StaffDeck 网页端重新生成后再试。"

    external_id = inbound.from_user_id
    identity = find_channel_identity(db, binding.channel, external_id)
    old_user_id = identity.staffdeck_user_id if identity else None
    if old_user_id and old_user_id != owner.id:
        current = db.get(User, old_user_id)
        if current and current.source == "web":
            display = current.display_name or current.username
            return f"该微信已绑定到 StaffDeck 账号「{display}」，请先发送 /解绑 解除后再绑定。"

    # ① 身份指针改指码主账号(无记录则新建)
    if identity:
        identity.staffdeck_user_id = owner.id
        identity.updated_at = utc_now()
    else:
        identity = ChannelIdentity(
            tenant_id=binding.tenant_id,
            channel=binding.channel,
            external_user_id=external_id,
            staffdeck_user_id=owner.id,
            display_name=owner.display_name,
        )
    db.add(identity)
    # ② 历史迁移:原懒建账号名下的渠道会话与全部记忆迁到码主账号
    if old_user_id and old_user_id != owner.id:
        _migrate_sessions(db, binding, from_user_id=old_user_id, to_user=owner)
        _migrate_memories(db, from_user_id=old_user_id, to_user=owner)
    # ③ 码核销
    record.used_at = now
    db.add(record)
    display = owner.display_name or owner.username
    return f"绑定成功，微信对话将与你的 StaffDeck 账号「{display}」共享记忆与对话记录。"


def _unbind_external_identity(
    db: Session,
    binding: ChannelBinding,
    inbound: ChannelInbound,
) -> str:
    current = unbind_external_identity(db, binding.tenant_id, binding.channel, inbound.from_user_id)
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

    with Session(use_engine) as db:
        event = ChannelInboundEvent(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            channel=binding.channel,
            event_id=inbound.event_id,
            payload_json=inbound.raw,
            status="processing",
        )
        db.add(event)
        try:
            db.commit()
        except IntegrityError:
            # (channel, event_id) 唯一冲突 = 已处理过，直接返回（幂等）
            db.rollback()
            return False

        # 指令拦截:早于身份解析与会话创建,指令消息不进 AgentLoop
        command = parse_command(inbound.text)
        target = {
            "to_user_id": inbound.conv_key if inbound.is_group else inbound.from_user_id,
            "context_token": inbound.context_token,
        }
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
        )
        user = resolve_or_provision_user(db, binding.tenant_id, binding.channel, external_id, display_name)
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
        if _user_message_with_client_turn_exists(db, chat_session.id, inbound.event_id):
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
