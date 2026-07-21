from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import ChannelBinding, ChatSession, User, new_id

_CHANNEL_TITLE_LIMIT = 20


def adopt_orphan_channel_sessions(db: Session, binding: ChannelBinding) -> int:
    """认领孤儿渠道会话(误删绑定后重绑自愈),返回认领数量。

    范围:本租户内同渠道、channel_binding_id 为空或指向已不存在绑定、
    agent_id 属于本绑定挂载集(含 binding.agent_id 回退)的会话;
    企微额外要求会话 conv 的 scope 前缀与本绑定一致,防误认领别家企业会话。
    """
    from app.channels.service_identity import external_account_scope
    from app.channels.service_routing import mounted_agents

    agent_ids = [mount.agent_id for mount in mounted_agents(db, binding)]
    scope_prefix = ""
    if binding.channel == "wecom":
        scope_prefix = f"wecom_{external_account_scope(db, binding)}_"
    alive_binding_ids = set(
        db.exec(
            select(ChannelBinding.id).where(ChannelBinding.tenant_id == binding.tenant_id)
        ).all()
    )
    candidates = db.exec(
        select(ChatSession).where(
            ChatSession.tenant_id == binding.tenant_id,
            ChatSession.channel == binding.channel,
            ChatSession.agent_id.in_(agent_ids),
        )
    ).all()
    adopted = 0
    for row in candidates:
        if row.channel_binding_id and row.channel_binding_id in alive_binding_ids:
            continue
        if scope_prefix and not (row.external_conv_id or "").startswith(scope_prefix):
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
