from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import ChannelBinding, ChatSession, User, new_id, utc_now

_CHANNEL_TITLE_LIMIT = 20


def migrate_binding_session_account_key(
    db: Session,
    binding_id: str,
    old_account_key: str | None,
    new_account_key: str,
) -> int:
    """首次补 corp_id 时把同一 Bot 的 legacy 会话锚点升级为复合账号键。"""
    if old_account_key == new_account_key:
        return 0
    rows = db.exec(
        select(ChatSession).where(ChatSession.channel_binding_id == binding_id)
    ).all()
    migrated = 0
    for row in rows:
        if row.channel_account_key not in {None, old_account_key}:
            continue
        row.channel_account_key = new_account_key
        db.add(row)
        migrated += 1
    return migrated


def adopt_orphan_channel_sessions(db: Session, binding: ChannelBinding) -> int:
    """仅按稳定外部账号键认领孤儿会话;无法确定归属的 legacy 会话保持孤立。"""
    from app.channels.service_routing import mounted_agents

    if not binding.external_account_key:
        return 0
    agent_ids = [mount.agent_id for mount in mounted_agents(db, binding)]
    alive_binding_ids = set(
        db.exec(
            select(ChannelBinding.id).where(ChannelBinding.tenant_id == binding.tenant_id)
        ).all()
    )
    candidates = db.exec(
        select(ChatSession).where(
            ChatSession.tenant_id == binding.tenant_id,
            ChatSession.channel == binding.channel,
            ChatSession.channel_account_key == binding.external_account_key,
            ChatSession.agent_id.in_(agent_ids),
        )
    ).all()
    adopted = 0
    for row in candidates:
        if row.channel_binding_id and row.channel_binding_id in alive_binding_ids:
            continue
        row.channel_binding_id = binding.id
        db.add(row)
        adopted += 1
    return adopted


def find_channel_session(
    db: Session,
    binding: ChannelBinding,
    agent_id: str,
    external_conv_id: str,
) -> ChatSession | None:
    return db.exec(
        select(ChatSession).where(
            ChatSession.agent_id == agent_id,
            ChatSession.channel == binding.channel,
            ChatSession.channel_binding_id == binding.id,
            ChatSession.external_conv_id == external_conv_id,
        )
    ).first()


def find_or_create_channel_session(
    db: Session,
    binding: ChannelBinding,
    user: User,
    agent_id: str,
    external_conv_id: str,
    first_text: str,
) -> ChatSession:
    """按 (agent_id, channel, external_conv_id) 锚定渠道会话，无则创建。"""
    chat_session = find_channel_session(db, binding, agent_id, external_conv_id)
    if chat_session:
        if chat_session.user_id != user.id:
            # 身份重绑或旧跨租户/跨企业污染：历史会话不可直接挂到新 User。
            # 改写 key 解除唯一约束后保留旧记录，新会话从干净上下文开始。
            chat_session.external_conv_id = (
                f"legacy_identity_mismatch:{chat_session.id}:{external_conv_id}"
            )
            chat_session.status = "archived"
            chat_session.channel_target_json = None
            chat_session.updated_at = utc_now()
            db.add(chat_session)
            db.flush()
            chat_session = None
        else:
            if not chat_session.channel_account_key and binding.external_account_key:
                chat_session.channel_account_key = binding.external_account_key
                db.add(chat_session)
            return chat_session

    title = (first_text or "").strip()[:_CHANNEL_TITLE_LIMIT] or None
    chat_session = ChatSession(
        id=new_id("session"),
        tenant_id=binding.tenant_id,
        user_id=user.id,
        agent_id=agent_id,
        title=title,
        channel=binding.channel,
        external_conv_id=external_conv_id,
        channel_binding_id=binding.id,
        channel_account_key=binding.external_account_key,
    )
    db.add(chat_session)
    try:
        db.flush()
    except IntegrityError:
        # 并发下另一线程已建会话：回滚后重查兜底
        db.rollback()
        chat_session = find_channel_session(db, binding, agent_id, external_conv_id)
        if chat_session:
            return chat_session
        raise
    return chat_session
