from __future__ import annotations

import hashlib
import logging
import re
import secrets

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import (
    ChannelBinding,
    ChannelConvState,
    ChannelIdentity,
    ChatSession,
    MemoryRecord,
    User,
    utc_now,
)
from app.security.auth import hash_password

logger = logging.getLogger(__name__)

_USERNAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_.@-]")

# 渠道显示名前缀(懒建账号 display_name 用)
_CHANNEL_LABELS = {"wechat": "微信", "wecom": "企微"}


class IdentityScopeConflict(RuntimeError):
    pass


def channel_label(channel: str) -> str:
    return _CHANNEL_LABELS.get(channel, channel)


def scope_from_config(config: dict, binding: ChannelBinding) -> str:
    """按配置计算生效 scope:wecom 取 corp_id/bot_id,兜底 binding.id;其他渠道置空。"""
    if binding.channel != "wecom":
        return ""
    return str(config.get("corp_id") or config.get("bot_id") or "").strip() or binding.id


def external_account_scope(db: Session, binding: ChannelBinding) -> str:
    """渠道账号作用域:以绑定当前配置为准(corp_id > bot_id > binding.id)。"""
    if binding.identity_scope_key is not None:
        return binding.identity_scope_key
    return scope_from_config(dict(binding.config_json or {}), binding)


def external_account_key(channel: str, config: dict) -> str | None:
    """provider Bot 的全部署稳定连接键。"""
    if channel == "wecom":
        corp_id = str(config.get("corp_id") or "").strip()
        bot_id = str(config.get("bot_id") or "").strip()
        if corp_id and bot_id:
            return f"wecom:corp:{len(corp_id)}:{corp_id}:bot:{len(bot_id)}:{bot_id}"
        # 仅兼容尚未补企业 ID 的存量 PR 数据；新激活必须提供 corp_id。
        return f"wecom:bot:{bot_id}" if bot_id else None
    if channel == "wechat":
        bot_id = str(config.get("ilink_bot_id") or "").strip()
        return f"wechat:ilink_bot:{bot_id}" if bot_id else None
    return None


def legacy_external_account_keys(channel: str, config: dict) -> set[str]:
    if channel != "wecom":
        return set()
    bot_id = str(config.get("bot_id") or "").strip()
    return {f"wecom:bot:{bot_id}"} if bot_id else set()


def p2p_external_conv_id(channel: str, account_scope: str, from_user_id: str) -> str:
    if account_scope:
        return f"{channel}_{account_scope}_p2p_{from_user_id}"
    return f"{channel}_p2p_{from_user_id}"


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
    account_scope: str = "",
) -> tuple[str, str]:
    """返回 (external_user_id, display_name)：群聊映射群账号，私聊映射个人账号。"""
    label = channel_label(channel)
    if is_group:
        # scope 已是身份唯一键的独立字段,subject id 只保留 provider 原始群 ID
        return f"group:{conv_key}", f"{label}群聊 {_id_suffix(conv_key, 4)}"
    return from_user_id, f"{label}用户 {_id_suffix(from_user_id, 8)}"


def channel_username(
    tenant_id: str,
    channel: str,
    external_id: str,
    account_scope: str = "",
) -> str:
    """懒建账号 username:可读段 + 完整身份键 (tenant, channel, scope, external_id) 的
    稳定 hash 后缀(清洗后同名/跨租户同 id/超长 id 均不撞),总长 ≤64。"""
    cleaned = _USERNAME_UNSAFE.sub("_", external_id)
    if account_scope:
        scope = _USERNAME_UNSAFE.sub("_", account_scope)[:24]
        readable = f"{channel}_{scope}_{cleaned}"
    else:
        readable = f"{channel}_{cleaned}"
    digest = hashlib.sha256(
        f"{tenant_id}:{channel}:{account_scope}:{external_id}".encode("utf-8")
    ).hexdigest()[:8]
    if len(readable) > 53:
        readable = readable[:53]
    return f"{readable}..{digest}"


def find_channel_identity(
    db: Session,
    tenant_id: str,
    channel: str,
    external_id: str,
    account_scope: str = "",
) -> ChannelIdentity | None:
    return db.exec(
        select(ChannelIdentity).where(
            ChannelIdentity.tenant_id == tenant_id,
            ChannelIdentity.channel == channel,
            ChannelIdentity.external_account_scope == account_scope,
            ChannelIdentity.external_user_id == external_id,
        )
    ).first()


def resolve_or_provision_user(
    db: Session,
    tenant_id: str,
    channel: str,
    external_id: str,
    display_name: str | None = None,
    account_scope: str = "",
) -> User:
    """按 (tenant, channel, scope, external_id) 解析 StaffDeck 用户，不存在则开通懒建账号。"""
    identity = find_channel_identity(db, tenant_id, channel, external_id, account_scope)
    if not identity and external_id.startswith("group:"):
        # 兼容 PR 早期数据:group_{scope}_{chatid} → group:{chatid}
        conv_key = external_id.removeprefix("group:")
        legacy_id = f"group_{account_scope}_{conv_key}" if account_scope else f"group_{conv_key}"
        legacy = find_channel_identity(db, tenant_id, channel, legacy_id, account_scope)
        if legacy:
            legacy.external_user_id = external_id
            legacy.updated_at = utc_now()
            db.add(legacy)
            db.flush()
            identity = legacy
    if identity:
        user = db.get(User, identity.staffdeck_user_id)
        if user and user.tenant_id == tenant_id:
            return user
        # 损坏或跨租户 identity 不能继续占据正常 scope 唯一键。
        db.delete(identity)
        db.flush()

    username = channel_username(tenant_id, channel, external_id, account_scope)
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
            external_account_scope=account_scope,
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
        identity = find_channel_identity(db, tenant_id, channel, external_id, account_scope)
        if identity:
            user = db.get(User, identity.staffdeck_user_id)
            if user and user.tenant_id == tenant_id:
                return user
            db.delete(identity)
            db.flush()
        user = db.exec(
            select(User).where(User.tenant_id == tenant_id, User.username == username)
        ).first()
        if user:
            db.add(
                ChannelIdentity(
                    tenant_id=tenant_id,
                    channel=channel,
                    external_account_scope=account_scope,
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
    account_scope: str = "",
) -> User | None:
    """解绑外部身份:指针移回懒建账号(缺则按原规则创建),迁回该私聊身份的渠道会话与对应记忆。

    返回原绑定的 web 账号;未绑定(无映射或映射不是 web 账号)返回 None。
    群身份(group: 开头)不属于个人,不应调用本函数。
    """
    identity = find_channel_identity(db, tenant_id, channel, external_id, account_scope)
    current = db.get(User, identity.staffdeck_user_id) if identity else None
    if not identity or not current or current.source != "web":
        return None

    lazy_username = channel_username(tenant_id, channel, external_id, account_scope)
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

    external_conv_id = p2p_external_conv_id(channel, account_scope, external_id)
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


def migrate_scope_for_binding(
    db: Session,
    binding: ChannelBinding,
    old_scope: str,
    new_scope: str,
) -> dict[str, int]:
    """绑定生效 scope 变化时的连续性迁移(同一事务内调用,范围限定当前 binding)。

    仅允许"首次补充企业信息"形态:旧 scope 是本 binding 派生值(binding.id 或
    config_json.bot_id)且新 scope 是 corp_id;corpA→corpB 等跨企业变更不迁移
    (端点应已拦截 400,此处防御性零操作)。
    - channel_identities:迁移旧 Bot scope 下全部身份,包括尚无会话的 /绑定 身份;
      目标 scope 已存在且指向不同 User 时整体中止,不自动合并账号与记忆。
    - sessions / channel_conv_states:仅当前 binding 的 wecom conv 旧前缀替换。
    返回各项迁移数量统计。
    """
    stats = {"identities": 0, "identities_conflicted": 0, "sessions": 0, "conv_states": 0}
    if old_scope == new_scope or binding.channel != "wecom":
        return stats
    config = dict(binding.config_json or {})
    derived_scopes = {binding.id, str(config.get("bot_id") or "").strip()} - {""}
    corp_id = str(config.get("corp_id") or "").strip()
    if not (old_scope in derived_scopes and corp_id and new_scope == corp_id):
        logger.warning(
            "拒绝跨企业 scope 迁移(仅允许 派生bot→corp_id 的首次补充):old=%s new=%s binding=%s",
            old_scope,
            new_scope,
            binding.id,
        )
        return stats

    identities = db.exec(
        select(ChannelIdentity).where(
            ChannelIdentity.tenant_id == binding.tenant_id,
            ChannelIdentity.channel == binding.channel,
            ChannelIdentity.external_account_scope == old_scope,
        )
    ).all()
    migration_rows: list[tuple[ChannelIdentity, str, ChannelIdentity | None]] = []
    for row in identities:
        new_external_id = row.external_user_id
        legacy_group_prefix = f"group_{old_scope}_"
        if new_external_id.startswith(legacy_group_prefix):
            new_external_id = f"group:{new_external_id.removeprefix(legacy_group_prefix)}"
        conflict = find_channel_identity(
            db, binding.tenant_id, binding.channel, new_external_id, new_scope
        )
        if conflict and conflict.id != row.id and conflict.staffdeck_user_id != row.staffdeck_user_id:
            raise IdentityScopeConflict(
                "目标企业中该渠道身份已关联其他 StaffDeck 用户: "
                f"external_user_id={new_external_id}"
            )
        migration_rows.append((row, new_external_id, conflict))

    for row, new_external_id, conflict in migration_rows:
        if conflict and conflict.id != row.id:
            stats["identities_conflicted"] += 1
            db.delete(row)
            continue
        row.external_account_scope = new_scope
        row.external_user_id = new_external_id
        row.updated_at = utc_now()
        db.add(row)
        stats["identities"] += 1

    for kind in ("p2p", "group"):
        old_prefix = f"{binding.channel}_{old_scope}_{kind}_"
        new_prefix = f"{binding.channel}_{new_scope}_{kind}_"
        sessions = db.exec(
            select(ChatSession).where(
                ChatSession.tenant_id == binding.tenant_id,
                ChatSession.channel == binding.channel,
                ChatSession.channel_binding_id == binding.id,
                ChatSession.external_conv_id.like(f"{old_prefix}%"),
            )
        ).all()
        for row in sessions:
            row.external_conv_id = new_prefix + row.external_conv_id[len(old_prefix):]
            db.add(row)
            stats["sessions"] += 1
        states = db.exec(
            select(ChannelConvState).where(
                ChannelConvState.tenant_id == binding.tenant_id,
                ChannelConvState.binding_id == binding.id,
                ChannelConvState.external_conv_id.like(f"{old_prefix}%"),
            )
        ).all()
        for row in states:
            row.external_conv_id = new_prefix + row.external_conv_id[len(old_prefix):]
            row.updated_at = utc_now()
            db.add(row)
            stats["conv_states"] += 1

    return stats
