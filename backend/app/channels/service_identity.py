from __future__ import annotations

import re
import secrets

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import ChannelIdentity, ChatSession, MemoryRecord, User, utc_now
from app.security.auth import hash_password

_USERNAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_.@-]")

# 渠道显示名前缀(懒建账号 display_name 用)
_CHANNEL_LABELS = {"wechat": "微信", "wecom": "企微"}


def channel_label(channel: str) -> str:
    return _CHANNEL_LABELS.get(channel, channel)


def _id_suffix(external_id: str, length: int) -> str:
    """取外部 ID 的辨识尾段：先去掉 @ 域名部分再截断。"""
    body = external_id.split("@", 1)[0]
    return body[-length:] if len(body) > length else body


def external_identity_for_message(
    channel: str,
    *,
    is_group: bool,
    conv_key: str,
    from_user_id: str,
) -> tuple[str, str]:
    """返回 (external_user_id, display_name)：群聊映射群账号，私聊映射个人账号。"""
    label = channel_label(channel)
    if is_group:
        return f"group_{conv_key}", f"{label}群聊 {_id_suffix(conv_key, 4)}"
    return from_user_id, f"{label}用户 {_id_suffix(from_user_id, 8)}"


def channel_username(channel: str, external_id: str) -> str:
    cleaned = _USERNAME_UNSAFE.sub("_", external_id)[:48]
    return f"{channel}_{cleaned}"[:64]


def find_channel_identity(db: Session, channel: str, external_id: str) -> ChannelIdentity | None:
    return db.exec(
        select(ChannelIdentity).where(
            ChannelIdentity.channel == channel,
            ChannelIdentity.external_user_id == external_id,
        )
    ).first()


def resolve_or_provision_user(
    db: Session,
    tenant_id: str,
    channel: str,
    external_id: str,
    display_name: str | None = None,
) -> User:
    """按渠道外部身份解析 StaffDeck 用户，不存在则开通 member 账号并写映射。"""
    identity = find_channel_identity(db, channel, external_id)
    if identity:
        user = db.get(User, identity.staffdeck_user_id)
        if user:
            return user

    username = channel_username(channel, external_id)
    user = User(
        tenant_id=tenant_id,
        username=username,
        display_name=(display_name or "").strip() or username,
        role="member",
        source=channel,
        password_hash=hash_password(secrets.token_urlsafe(24)),
    )
    db.add(user)
    db.add(
        ChannelIdentity(
            tenant_id=tenant_id,
            channel=channel,
            external_user_id=external_id,
            staffdeck_user_id=user.id,
            display_name=user.display_name,
        )
    )
    try:
        db.flush()
    except IntegrityError:
        # 并发下另一线程已建号/已映射：回滚后按既有记录兜底
        db.rollback()
        identity = find_channel_identity(db, channel, external_id)
        if identity:
            user = db.get(User, identity.staffdeck_user_id)
            if user:
                return user
        user = db.exec(
            select(User).where(User.tenant_id == tenant_id, User.username == username)
        ).first()
        if user:
            db.add(
                ChannelIdentity(
                    tenant_id=tenant_id,
                    channel=channel,
                    external_user_id=external_id,
                    staffdeck_user_id=user.id,
                    display_name=user.display_name,
                )
            )
            db.flush()
            return user
        raise
    return user


def unbind_external_identity(
    db: Session,
    tenant_id: str,
    channel: str,
    external_id: str,
) -> User | None:
    """解绑外部身份:指针移回懒建账号(缺则按原规则创建),迁回该私聊身份的渠道会话与对应记忆。

    返回原绑定的 web 账号;未绑定(无映射或映射不是 web 账号)返回 None。
    群身份(group_ 开头)不属于个人,不应调用本函数。
    """
    identity = find_channel_identity(db, channel, external_id)
    current = db.get(User, identity.staffdeck_user_id) if identity else None
    if not identity or not current or current.source != "web":
        return None

    lazy_username = channel_username(channel, external_id)
    lazy = db.exec(
        select(User).where(User.tenant_id == tenant_id, User.username == lazy_username)
    ).first()
    if not lazy:
        _, lazy_display = external_identity_for_message(
            channel,
            is_group=False,
            conv_key="",
            from_user_id=external_id,
        )
        lazy = User(
            tenant_id=tenant_id,
            username=lazy_username,
            display_name=lazy_display,
            role="member",
            source=channel,
            password_hash=hash_password(secrets.token_urlsafe(24)),
        )
        db.add(lazy)
        db.flush()
    identity.staffdeck_user_id = lazy.id
    identity.updated_at = utc_now()
    db.add(identity)

    external_conv_id = f"{channel}_p2p_{external_id}"
    sessions = db.exec(
        select(ChatSession).where(
            ChatSession.user_id == current.id,
            ChatSession.channel == channel,
            ChatSession.external_conv_id == external_conv_id,
        )
    ).all()
    session_ids: set[str] = set()
    for row in sessions:
        row.user_id = lazy.id
        db.add(row)
        session_ids.add(row.id)
    if session_ids:
        memories = db.exec(
            select(MemoryRecord).where(
                MemoryRecord.user_id == current.id,
                MemoryRecord.session_id.in_(session_ids),
            )
        ).all()
        for row in memories:
            row.user_id = lazy.id
            row.username = lazy.username
            db.add(row)
    return current
