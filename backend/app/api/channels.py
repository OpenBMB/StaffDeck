from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import case, text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.channels import (
    binding_lifecycle_lock,
    channel_services_enabled,
    pause_binding_ingress,
    resume_binding_ingress,
    wait_binding_ingress_stopped,
)
from app.channels.adapters.dingtalk import (
    DingTalkPermanentError,
    validate_dingtalk_credentials,
)
from app.channels.adapters.discord import (
    DiscordPermanentError,
    validate_discord_credentials,
)
from app.channels.adapters.feishu import (
    FeishuPermanentError,
    validate_feishu_credentials,
)
from app.channels.adapters.wechat import WeChatClient, sanitize_wechat_baseurl, validate_wechat_host
from app.channels.batch_service import batch_service
from app.channels.crypto import decrypt_channel_secret, encrypt_channel_secret
from app.channels.schema import (
    BackfillJobRead,
    BackfillRequest,
    BatchJobRead,
    BatchSendRequest,
    BatchSubmitRead,
    ChannelBindCodeRead,
    ChannelBindingAgentRead,
    ChannelBindingAgentsUpdate,
    ChannelBindingCreate,
    ChannelBindingRead,
    ChannelConversationAttachmentRead,
    ChannelConversationMessageRead,
    ChannelConversationPage,
    ChannelConversationRead,
    ChannelDeliveryDay,
    ChannelDeliveryDayPage,
    ChannelDeliveryPage,
    ChannelMetaRead,
    ChannelQRCodeRead,
    ChannelQRCodeStatusRead,
    DingTalkCredentialsRequest,
    DiscordCredentialsRequest,
    FeishuCredentialsRequest,
    MyIdentityBindingRead,
    WeComCredentialsRequest,
    channel_binding_agents_read,
    channel_binding_read,
    channel_delivery_read,
)
from app.channels.service_identity import (
    IdentityScopeConflict,
    external_account_key,
    legacy_external_account_keys,
    migrate_scope_for_binding,
    scope_from_config,
    unbind_external_identity,
)
from app.channels.service_session import (
    adopt_orphan_channel_sessions,
    migrate_binding_session_account_key,
)
from app.config import get_settings
from app.db import get_session
from app.db.models import (
    AgentProfile,
    ChannelBindCode,
    ChannelBinding,
    ChannelBindingAgent,
    ChannelConvState,
    ChannelDelivery,
    ChannelIdentity,
    ChannelInboundEvent,
    ChatSession,
    Message,
    Team,
    User,
    utc_now,
)
from app.security.auth import get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_current_user_tenant,
    is_admin_user,
    require_agent_scope_viewer,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/enterprise/channels", tags=["enterprise:channels"])


def _patch_binding_config_key(
    db: Session,
    tenant_id: str,
    binding_id: str,
    key: str,
    value: object,
) -> None:
    """Patch one API-owned config key against the latest JSON value."""
    result = db.exec(
        text(
            "UPDATE channel_bindings "
            "SET config_json = json_set(COALESCE(config_json, '{}'), :path, json(:value)), "
            "updated_at = :updated_at "
            "WHERE id = :binding_id AND tenant_id = :tenant_id"
        ),
        params={
            "path": f"$.{key}",
            "value": json.dumps(value, ensure_ascii=False),
            "updated_at": utc_now(),
            "binding_id": binding_id,
            "tenant_id": tenant_id,
        },
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=404, detail="渠道绑定不存在")

SUPPORTED_CHANNELS = {"wechat", "wecom", "feishu", "dingtalk", "discord"}
INGRESS_QUIESCE_TIMEOUT_SECONDS = 5.0

# 渠道描述:前端接入页据此渲染渠道卡片与凭证表单,新渠道只加条目不动页面骨架
CHANNEL_META = [
    {
        "channel": "wechat",
        "name": "微信",
        "setup": "qrcode",
        "credential_fields": [],
        "capabilities": ["typing"],
    },
    {
        "channel": "wecom",
        "name": "企业微信",
        "setup": "credentials",
        "credential_fields": [
            {"key": "bot_id", "label": "机器人 ID", "placeholder": "企业微信后台获取", "secret": False},
            {"key": "secret", "label": "机器人 Secret", "placeholder": None, "secret": True},
            {
                "key": "corp_id",
                "label": "企业 ID",
                "placeholder": "管理后台-我的企业-企业信息",
                "secret": False,
                "optional": False,
            },
        ],
        "capabilities": [],
    },
    {
        "channel": "feishu",
        "name": "飞书",
        "setup": "credentials",
        "credential_fields": [
            {"key": "app_id", "label": "App ID", "placeholder": "cli_xxx", "secret": False},
            {"key": "app_secret", "label": "App Secret", "placeholder": None, "secret": True},
        ],
        "capabilities": [],
    },
    {
        "channel": "dingtalk",
        "name": "钉钉",
        "setup": "credentials",
        "credential_fields": [
            {"key": "client_id", "label": "Client ID", "placeholder": "钉钉开放平台获取", "secret": False},
            {"key": "client_secret", "label": "Client Secret", "placeholder": None, "secret": True},
        ],
        "capabilities": [],
    },
    {
        "channel": "discord",
        "name": "Discord",
        "setup": "credentials",
        "credential_fields": [
            {"key": "bot_token", "label": "Bot Token", "placeholder": "Discord Developer Portal 获取", "secret": True},
        ],
        "capabilities": [
            "slash_commands",
            "threads",
            "auto_thread",
            "batch_send",
            "backfill",
            "typing",
            "voice",
            "rich_media",
        ],
    },
]


def _get_binding(db: Session, tenant_id: str, binding_id: str) -> ChannelBinding:
    binding = db.get(ChannelBinding, binding_id)
    if not binding or binding.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Channel binding not found")
    return binding


def _ensure_binding_manager(db: Session, tenant_id: str, binding: ChannelBinding, current_user: User) -> None:
    """渠道绑定管理权限:仅 admin 或绑定创建者;不随默认员工(binding.agent_id)漂移。"""
    ensure_current_user_tenant(tenant_id, current_user)
    if is_admin_user(current_user) or binding.created_by_user_id == current_user.id:
        return
    raise HTTPException(status_code=403, detail="Only the creator or administrator can manage this channel binding")


def _ensure_external_account_available(
    db: Session,
    account_key: str,
    binding_id: str,
    aliases: set[str] | None = None,
) -> None:
    account_keys = {account_key, *(aliases or set())}
    conflict = db.exec(
        select(ChannelBinding).where(
            ChannelBinding.external_account_key.in_(account_keys),
            ChannelBinding.id != binding_id,
        )
    ).first()
    if conflict:
        raise HTTPException(status_code=409, detail="该外部机器人已被其他渠道绑定使用")


def _quiesce_binding_or_409(
    channel: str,
    binding_id: str,
    *,
    should_run: bool,
) -> None:
    """暂停并等待旧代际；调用前必须结束当前数据库事务。"""
    if not channel_services_enabled():
        return
    pause_binding_ingress(channel, binding_id)
    if wait_binding_ingress_stopped(
        channel,
        binding_id,
        INGRESS_QUIESCE_TIMEOUT_SECONDS,
    ):
        return
    # 旧 worker 仍在收敛,解除 pause 后由 reconcile 按数据库旧配置恢复
    resume_binding_ingress(channel, binding_id, start=False)
    raise HTTPException(status_code=409, detail="渠道仍有消息正在处理，请稍后重试")


def _resume_binding(channel: str, binding_id: str, *, start: bool) -> None:
    if channel_services_enabled():
        resume_binding_ingress(channel, binding_id, start=start)


def _ensure_revision(binding: ChannelBinding, expected_revision: int) -> None:
    if binding.config_revision != expected_revision:
        raise HTTPException(status_code=409, detail="渠道配置已被其他请求修改，请重试")


@router.get("/meta", response_model=list[ChannelMetaRead])
def list_channel_meta(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
) -> list[ChannelMetaRead]:
    """渠道描述清单:前端接入页按此渲染渠道卡片与凭证表单(任意登录用户)。"""
    ensure_current_user_tenant(tenant_id, current_user)
    return [ChannelMetaRead.model_validate(item) for item in CHANNEL_META]


@router.get("", response_model=list[ChannelBindingRead])
def list_channel_bindings(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ChannelBindingRead]:
    if agent_id:
        require_agent_scope_viewer(tenant_id, agent_id, current_user, db)
    else:
        ensure_current_user_tenant(tenant_id, current_user)
    statement = select(ChannelBinding).where(ChannelBinding.tenant_id == tenant_id)
    if agent_id:
        statement = statement.where(ChannelBinding.agent_id == agent_id)
    elif not is_admin_user(current_user):
        # 渠道绑定是租户级资源:admin 全量可见,普通成员只见自己创建的
        statement = statement.where(ChannelBinding.created_by_user_id == current_user.id)
    rows = db.exec(statement.order_by(ChannelBinding.created_at)).all()
    return [channel_binding_read(db, row) for row in rows]


@router.post("", response_model=ChannelBindingRead)
def create_channel_binding(
    request: ChannelBindingCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelBindingRead:
    ensure_current_user_tenant(request.tenant_id, current_user)
    if request.channel not in SUPPORTED_CHANNELS:
        raise HTTPException(status_code=400, detail=f"v1 仅支持渠道: {sorted(SUPPORTED_CHANNELS)}")
    # 挂员工集或绑团队二选一:都给/都不给均拒绝
    if bool(request.agent_id) == bool(request.team_id):
        raise HTTPException(status_code=400, detail="agent_id 与 team_id 必须且只能提供一个")
    if request.team_id:
        team = db.get(Team, request.team_id)
        if team is None or team.tenant_id != request.tenant_id:
            raise HTTPException(status_code=404, detail="Team not found")
        from app.teams.service import get_team_leader

        leader = get_team_leader(db, team.id)
        if leader is None:
            raise HTTPException(status_code=400, detail="团队暂未设置 TL，请先设置 TL 后再绑定渠道")
        # 复用员工绑定同款守卫:创建者须能管理现任 TL 员工
        ensure_agent_scope_manager(db, request.tenant_id, leader.agent_id, current_user)
        # agent_id 为非空遗留列(列表过滤/挂载回退仍在用):团队绑定回写现任 TL,
        # 入站路由始终以 binding.team_id 解析的现任 TL 为准,换帅自动跟随
        target_agent_id = leader.agent_id
    else:
        ensure_agent_scope_manager(db, request.tenant_id, request.agent_id, current_user)
        target_agent_id = request.agent_id
    # 同一员工同一渠道允许多个绑定实例,总是新建
    binding = ChannelBinding(
        tenant_id=request.tenant_id,
        agent_id=target_agent_id,
        channel=request.channel,
        status="pending",
        created_by_user_id=current_user.id,
        team_id=request.team_id,
    )
    db.add(binding)
    db.flush()
    if not request.team_id:
        # 新绑定自动挂载默认员工;团队绑定走 TL 直路由,不写挂载行
        db.add(
            ChannelBindingAgent(
                tenant_id=request.tenant_id,
                binding_id=binding.id,
                agent_id=target_agent_id,
                is_default=True,
                sort_order=0,
            )
        )
    db.commit()
    db.refresh(binding)
    return channel_binding_read(db, binding)


BIND_CODE_TTL_MINUTES = 10
# bind-code 生成端限速:同一用户每分钟最多 5 次(进程内滑动窗口,重启清零)
_BIND_CODE_RATE_LIMIT = 5
_BIND_CODE_RATE_WINDOW_SECONDS = 60.0
_bind_code_requests: dict[str, list[float]] = {}
_bind_code_requests_lock = threading.Lock()


def _check_bind_code_rate(user_id: str) -> bool:
    """滑动窗口限速检查并计数;超限返回 False。"""
    now = time.monotonic()
    with _bind_code_requests_lock:
        window = [
            at
            for at in _bind_code_requests.get(user_id, [])
            if now - at < _BIND_CODE_RATE_WINDOW_SECONDS
        ]
        if len(window) >= _BIND_CODE_RATE_LIMIT:
            _bind_code_requests[user_id] = window
            return False
        window.append(now)
        _bind_code_requests[user_id] = window
        return True


def _generate_bind_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


@router.post("/bind-code", response_model=ChannelBindCodeRead)
def create_bind_code(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelBindCodeRead:
    """为当前用户生成渠道身份绑定码(6 位数字,10 分钟有效,旧码作废)。"""
    ensure_current_user_tenant(tenant_id, current_user)
    if not _check_bind_code_rate(current_user.id):
        raise HTTPException(status_code=429, detail="绑定码生成过于频繁，请稍后再试")
    user_id = current_user.id
    for _attempt in range(10):
        now = utc_now()
        record = db.exec(
            select(ChannelBindCode).where(
                ChannelBindCode.tenant_id == tenant_id,
                ChannelBindCode.user_id == user_id,
            )
        ).first()
        if record:
            record.code = _generate_bind_code()
            record.expires_at = now + timedelta(minutes=BIND_CODE_TTL_MINUTES)
            record.used_at = None
            record.created_at = now
        else:
            record = ChannelBindCode(
                tenant_id=tenant_id,
                user_id=user_id,
                code=_generate_bind_code(),
                expires_at=now + timedelta(minutes=BIND_CODE_TTL_MINUTES),
            )
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        return ChannelBindCodeRead(code=record.code, expires_at=record.expires_at.isoformat())
    raise HTTPException(status_code=409, detail="绑定码生成冲突，请重试")


@router.get("/my-identity-bindings", response_model=list[MyIdentityBindingRead])
def list_my_identity_bindings(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[MyIdentityBindingRead]:
    """当前用户的渠道身份绑定状态(任意登录用户可见自己的)。"""
    ensure_current_user_tenant(tenant_id, current_user)
    rows = db.exec(
        select(ChannelIdentity)
        .where(
            ChannelIdentity.tenant_id == tenant_id,
            ChannelIdentity.staffdeck_user_id == current_user.id,
        )
        .order_by(ChannelIdentity.channel)
    ).all()
    return [
        MyIdentityBindingRead(
            channel=row.channel,
            external_user_id=row.external_user_id,
            display_name=row.display_name,
            bound_at=row.updated_at.isoformat(),
            external_account_scope=row.external_account_scope,
        )
        for row in rows
    ]


@router.delete("/my-identity-bindings/{channel}", status_code=204)
def delete_my_identity_binding(
    channel: str,
    tenant_id: str = Query(...),
    external_user_id: str | None = Query(None),
    external_account_scope: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> Response:
    """页面侧解除当前用户在指定渠道的身份绑定(效果同 /解绑 指令)。

    传 external_user_id 时只解绑该外部身份:同时传 external_account_scope 按完整
    身份键精确定位一行;未传 scope 而该外部身份在多 scope(多企业)下均有绑定时
    返回 400 要求指定 scope,不盲删。未传 external_user_id 时按 channel 全部解绑。
    """
    ensure_current_user_tenant(tenant_id, current_user)
    statement = select(ChannelIdentity).where(
        ChannelIdentity.tenant_id == tenant_id,
        ChannelIdentity.channel == channel,
        ChannelIdentity.staffdeck_user_id == current_user.id,
    )
    if external_user_id:
        statement = statement.where(ChannelIdentity.external_user_id == external_user_id)
        if external_account_scope is not None:
            statement = statement.where(
                ChannelIdentity.external_account_scope == external_account_scope
            )
    identities = db.exec(statement).all()
    if not identities:
        raise HTTPException(status_code=404, detail="Identity binding not found")
    if external_user_id and external_account_scope is None:
        scopes = {identity.external_account_scope for identity in identities}
        if len(scopes) > 1:
            raise HTTPException(
                status_code=400,
                detail="该外部身份在多个企业账号下均有绑定，请指定 external_account_scope 后再解绑",
            )
    for identity in identities:
        unbind_external_identity(
            db, tenant_id, channel, identity.external_user_id, identity.external_account_scope
        )
    db.commit()
    return Response(status_code=204)


@router.get("/{binding_id}/agents", response_model=list[ChannelBindingAgentRead])
def list_channel_binding_agents(
    binding_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ChannelBindingAgentRead]:
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    return channel_binding_agents_read(db, binding)


@router.put("/{binding_id}", response_model=ChannelBindingRead)
def update_channel_binding_agents(
    binding_id: str,
    request: ChannelBindingAgentsUpdate,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelBindingRead:
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    if (
        request.agents is None
        and request.auto_route is None
        and request.allowlist is None
        and request.features is None
    ):
        raise HTTPException(status_code=400, detail="无有效更新内容")
    if request.agents is not None and binding.team_id:
        # 团队绑定的接待员工由团队现任 TL 决定,不允许整表替换员工挂载
        raise HTTPException(status_code=400, detail="团队绑定的渠道不支持修改员工挂载")
    default_agent_id: str | None = None
    if request.agents is not None:
        if not request.agents:
            raise HTTPException(status_code=400, detail="挂载员工列表不能为空")
        seen: set[str] = set()
        for item in request.agents:
            if item.agent_id in seen:
                raise HTTPException(status_code=400, detail="挂载员工列表存在重复")
            seen.add(item.agent_id)
            # 逐员工作 manager 校验;未知员工由该校验抛 404
            ensure_agent_scope_manager(db, tenant_id, item.agent_id, current_user)
        # 恰好一个默认:未标则取第一个,多标取首个标记
        marked = [item.agent_id for item in request.agents if item.is_default]
        default_agent_id = marked[0] if marked else request.agents[0].agent_id
    db.rollback()
    with binding_lifecycle_lock(binding_id):
        binding = _get_binding(db, tenant_id, binding_id)
        if request.agents is not None:
            existing = db.exec(
                select(ChannelBindingAgent).where(ChannelBindingAgent.binding_id == binding.id)
            ).all()
            for row in existing:
                db.delete(row)
            db.flush()
            for index, item in enumerate(request.agents):
                db.add(
                    ChannelBindingAgent(
                        tenant_id=tenant_id,
                        binding_id=binding.id,
                        agent_id=item.agent_id,
                        is_default=item.agent_id == default_agent_id,
                        sort_order=index,
                    )
                )
            binding.agent_id = default_agent_id
        binding.updated_at = utc_now()
        db.add(binding)
        db.commit()
        if request.auto_route is not None:
            _patch_binding_config_key(
                db,
                tenant_id,
                binding_id,
                "auto_route",
                request.auto_route,
            )
            db.commit()
        if request.allowlist is not None or request.features is not None:
            # 配置结构化段(allowlist/features)变更 → config_revision+=1,
            # _reconcile_loop 按 revision 条件 5s 内重建线程/刷新 fence 缓存
            binding = _get_binding(db, tenant_id, binding_id)
            config = dict(binding.config_json or {})
            if request.allowlist is not None:
                config["allowlist"] = request.allowlist.model_dump()
            if request.features is not None:
                config["features"] = request.features.model_dump()
            binding.config_json = config
            binding.config_revision += 1
            binding.updated_at = utc_now()
            db.add(binding)
            db.commit()
        binding = _get_binding(db, tenant_id, binding_id)
        db.refresh(binding)
        return channel_binding_read(db, binding)


@router.delete("/{binding_id}", status_code=204)
def delete_channel_binding(
    binding_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> Response:
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    db.rollback()
    with binding_lifecycle_lock(binding_id):
        binding = _get_binding(db, tenant_id, binding_id)
        expected_revision = binding.config_revision
        channel = binding.channel
        should_run = bool(binding.status == "active" and binding.credentials_enc)
        db.rollback()
        _quiesce_binding_or_409(channel, binding_id, should_run=should_run)
        from app.channels.service_intake import pause_binding_intake, resume_binding_intake

        if not pause_binding_intake(binding_id, INGRESS_QUIESCE_TIMEOUT_SECONDS):
            resume_binding_intake(binding_id)
            _resume_binding(channel, binding_id, start=should_run)
            raise HTTPException(status_code=409, detail="渠道消息仍在处理中，请稍后重试删除")
        try:
            binding = _get_binding(db, tenant_id, binding_id)
            _ensure_revision(binding, expected_revision)
            from app.channels.service_outbox import (
                cleanup_channel_reactions_before_binding_delete,
            )

            try:
                # 渠道不支持 reaction 时该调用自身即为空操作。
                cleanup_channel_reactions_before_binding_delete(db, binding)
            except Exception as exc:
                logger.warning(
                    "删除渠道绑定前清理 reaction 失败 channel=%s binding=%s: %s",
                    binding.channel,
                    binding.id,
                    exc,
                )
                raise HTTPException(
                    status_code=409,
                    detail="渠道消息确认尚未清理，请稍后重试删除",
                ) from exc
            db.exec(
                update(ChannelDelivery)
                .where(
                    ChannelDelivery.tenant_id == tenant_id,
                    ChannelDelivery.binding_id == binding.id,
                    ChannelDelivery.status.in_({"pending", "sending"}),
                )
                .values(
                    status="failed",
                    next_attempt_at=None,
                    last_error=case(
                        (ChannelDelivery.status == "sending", "binding_deleted_remote_unknown"),
                        else_="binding_deleted",
                    ),
                    updated_at=utc_now(),
                )
            )
            db.exec(
                update(ChannelInboundEvent)
                .where(
                    ChannelInboundEvent.tenant_id == tenant_id,
                    ChannelInboundEvent.binding_id == binding.id,
                    ChannelInboundEvent.status.in_({"received", "processing"}),
                )
                .values(
                    status="failed",
                    processor_run_id=None,
                    error=case(
                        (
                            ChannelInboundEvent.status == "processing",
                            "binding_deleted_incomplete_turn",
                        ),
                        else_="binding_deleted",
                    ),
                    updated_at=utc_now(),
                )
            )
            # 同事务级联删除挂载行与路由指针
            for mount in db.exec(
                select(ChannelBindingAgent).where(ChannelBindingAgent.binding_id == binding.id)
            ).all():
                db.delete(mount)
            for state in db.exec(
                select(ChannelConvState).where(ChannelConvState.binding_id == binding.id)
            ).all():
                db.delete(state)
            db.delete(binding)
            db.commit()
        except Exception:
            db.rollback()
            resume_binding_intake(binding_id)
            _resume_binding(channel, binding_id, start=should_run)
            raise
        _resume_binding(channel, binding_id, start=False)
    return Response(status_code=204)


@router.post("/{binding_id}/wechat/qrcode", response_model=ChannelQRCodeRead)
def create_wechat_qrcode(
    binding_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelQRCodeRead:
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    # 官方协议:local_token_list 带上本地已有 bot_token(最多 10 个),支持旧凭证续绑
    local_tokens: list[str] = []
    if binding.credentials_enc:
        try:
            local_tokens = [decrypt_channel_secret(binding.credentials_enc)]
        except Exception:
            logger.warning("解密已有渠道凭证失败,按无凭证申请二维码 binding=%s", binding_id)
    db.rollback()
    client = WeChatClient(get_settings().wechat_ilink_base_url)
    try:
        data = client.get_bot_qrcode(local_token_list=local_tokens)
    except Exception as exc:
        logger.warning("获取微信二维码失败 binding=%s: %s", binding_id, exc)
        raise HTTPException(status_code=502, detail="获取微信二维码失败，请稍后重试") from exc
    qrcode = str(data.get("qrcode") or "")
    if not qrcode:
        raise HTTPException(status_code=502, detail="微信二维码接口返回异常")
    return ChannelQRCodeRead(qrcode=qrcode, qrcode_img_content=data.get("qrcode_img_content"))


def _activate_binding_with_existing_credentials(
    db: Session,
    binding: ChannelBinding,
) -> ChannelBinding:
    binding_id = binding.id
    tenant_id = binding.tenant_id
    channel = binding.channel
    db.rollback()
    with binding_lifecycle_lock(binding_id):
        binding = _get_binding(db, tenant_id, binding_id)
        expected_revision = binding.config_revision
        should_run = bool(binding.status == "active" and binding.credentials_enc)
        config = dict(binding.config_json or {})
        account_key = external_account_key(binding.channel, config)
        if not account_key:
            raise HTTPException(status_code=409, detail="已有渠道凭证缺少外部机器人标识")
        _ensure_external_account_available(db, account_key, binding.id)
        db.rollback()
        _quiesce_binding_or_409(channel, binding_id, should_run=should_run)
        try:
            binding = _get_binding(db, tenant_id, binding_id)
            _ensure_revision(binding, expected_revision)
            _ensure_external_account_available(db, account_key, binding_id)
            config.pop("qrcode_redirect_baseurl", None)
            config["session_expired"] = False
            config["get_updates_buf"] = ""
            binding.config_json = config
            binding.external_account_key = account_key
            binding.identity_scope_key = ""
            binding.config_revision += 1
            binding.status = "active"
            binding.connected = False
            binding.updated_at = utc_now()
            db.add(binding)
            db.commit()
            db.refresh(binding)
        except IntegrityError as exc:
            db.rollback()
            _resume_binding(channel, binding_id, start=should_run)
            raise HTTPException(status_code=409, detail="该外部机器人已被其他渠道绑定使用") from exc
        except Exception:
            db.rollback()
            _resume_binding(channel, binding_id, start=should_run)
            raise
        _resume_binding(channel, binding_id, start=True)
        return binding


@router.get("/{binding_id}/wechat/qrcode-status", response_model=ChannelQRCodeStatusRead)
def poll_wechat_qrcode_status(
    binding_id: str,
    qrcode: str,
    tenant_id: str = Query(...),
    verify_code: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelQRCodeStatusRead:
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    redirect_baseurl = str((binding.config_json or {}).get("qrcode_redirect_baseurl") or "").strip()
    has_credentials = bool(binding.credentials_enc)
    db.rollback()
    client = WeChatClient(
        sanitize_wechat_baseurl(
            redirect_baseurl or get_settings().wechat_ilink_base_url,
            default=get_settings().wechat_ilink_base_url,
        )
    )
    try:
        data = client.get_qrcode_status(qrcode, verify_code=verify_code)
    except Exception as exc:
        logger.warning("轮询微信扫码状态失败 binding=%s: %s", binding_id, exc)
        raise HTTPException(status_code=502, detail="轮询微信扫码状态失败，请重试") from exc
    status = str(data.get("status") or "wait")
    if status == "scaned_but_redirect":
        # 扫码后被要求切换接入域名:域名必须属于腾讯官方域,否则不存不用(防凭证外发)
        redirect_host = str(data.get("redirect_host") or "").strip()
        if not redirect_host or not validate_wechat_host(redirect_host):
            logger.warning("微信 redirect_host 不受信任,拒绝使用 binding=%s host=%s", binding_id, redirect_host)
            raise HTTPException(status_code=502, detail="微信返回的接入域名不受信任，请刷新二维码重试")
        db.rollback()
        with binding_lifecycle_lock(binding_id):
            _get_binding(db, tenant_id, binding_id)
            db.rollback()
            _patch_binding_config_key(
                db,
                tenant_id,
                binding_id,
                "qrcode_redirect_baseurl",
                f"https://{redirect_host.lower()}",
            )
            db.commit()
        return ChannelQRCodeStatusRead(status=status)
    if status == "binded_redirect":
        # 该 bot 已绑定过本实例,旧凭证仍有效:直接复用激活
        if has_credentials:
            binding = _get_binding(db, tenant_id, binding_id)
            binding = _activate_binding_with_existing_credentials(db, binding)
            return ChannelQRCodeStatusRead(status="confirmed", binding=channel_binding_read(db, binding))
        return ChannelQRCodeStatusRead(status=status)
    if status != "confirmed":
        # wait/scaned/expired/need_verifycode/verify_code_blocked 等原样透传
        return ChannelQRCodeStatusRead(status=status)

    bot_token = str(data.get("bot_token") or "")
    if not bot_token:
        raise HTTPException(status_code=502, detail="微信扫码确认返回缺少凭证")
    ilink_bot_id = str(data.get("ilink_bot_id") or "").strip()
    if not ilink_bot_id:
        raise HTTPException(status_code=502, detail="微信扫码确认返回缺少机器人标识")
    db.rollback()
    with binding_lifecycle_lock(binding_id):
        binding = _get_binding(db, tenant_id, binding_id)
        expected_revision = binding.config_revision
        channel = binding.channel
        should_run = bool(binding.status == "active" and binding.credentials_enc)
        old_config = dict(binding.config_json or {})
        old_account_key = binding.external_account_key or external_account_key(channel, old_config)
        config = dict(binding.config_json or {})
        config.pop("qrcode_redirect_baseurl", None)
        config.update(
            {
                "ilink_bot_id": ilink_bot_id,
                "ilink_user_id": str(data.get("ilink_user_id") or ""),
                "baseurl": sanitize_wechat_baseurl(
                    str(data.get("baseurl") or "") or get_settings().wechat_ilink_base_url,
                    default=get_settings().wechat_ilink_base_url,
                ),
                "get_updates_buf": "",
                "session_expired": False,
                "bound_at": utc_now().isoformat(),
            }
        )
        account_key = external_account_key(binding.channel, config)
        if not account_key:
            raise HTTPException(status_code=502, detail="微信扫码确认返回缺少机器人标识")
        if old_account_key and old_account_key != account_key:
            raise HTTPException(
                status_code=400,
                detail="机器人变更不允许直接修改，请删除后重新创建绑定",
            )
        _ensure_external_account_available(db, account_key, binding.id)
        db.rollback()
        _quiesce_binding_or_409(channel, binding_id, should_run=should_run)
        try:
            binding = _get_binding(db, tenant_id, binding_id)
            _ensure_revision(binding, expected_revision)
            _ensure_external_account_available(db, account_key, binding_id)
            binding.credentials_enc = encrypt_channel_secret(bot_token)
            binding.config_json = config
            binding.external_account_key = account_key
            binding.identity_scope_key = ""
            binding.config_revision += 1
            binding.status = "active"
            binding.connected = False
            binding.updated_at = utc_now()
            db.add(binding)
            adopt_orphan_channel_sessions(db, binding)
            db.commit()
            db.refresh(binding)
        except IntegrityError as exc:
            db.rollback()
            _resume_binding(channel, binding_id, start=should_run)
            raise HTTPException(status_code=409, detail="该外部机器人已被其他渠道绑定使用") from exc
        except Exception:
            db.rollback()
            _resume_binding(channel, binding_id, start=should_run)
            raise
        _resume_binding(channel, binding_id, start=True)
    return ChannelQRCodeStatusRead(status=status, binding=channel_binding_read(db, binding))


@router.post("/{binding_id}/wecom/credentials", response_model=ChannelBindingRead)
def save_wecom_credentials(
    binding_id: str,
    request: WeComCredentialsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelBindingRead:
    """保存企微智能机器人凭证(bot_id + secret),激活绑定并拉起长连接。"""
    ensure_current_user_tenant(request.tenant_id, current_user)
    binding = _get_binding(db, request.tenant_id, binding_id)
    _ensure_binding_manager(db, request.tenant_id, binding, current_user)
    if binding.channel != "wecom":
        raise HTTPException(status_code=400, detail="该绑定不是企业微信渠道")
    bot_id = request.bot_id.strip()
    secret = request.secret.strip()
    corp_id = request.corp_id.strip()
    if not bot_id or not secret or not corp_id:
        raise HTTPException(status_code=400, detail="corp_id、bot_id 与 secret 均不能为空")
    db.rollback()
    with binding_lifecycle_lock(binding_id):
        binding = _get_binding(db, request.tenant_id, binding_id)
        expected_revision = binding.config_revision
        channel = binding.channel
        should_run = bool(binding.status == "active" and binding.credentials_enc)
        old_config = dict(binding.config_json or {})
        old_bot_id = str(old_config.get("bot_id") or "").strip()
        old_corp_id = str(old_config.get("corp_id") or "").strip()
        old_scope = scope_from_config(old_config, binding)
        old_account_key = binding.external_account_key or external_account_key(channel, old_config)
        if old_corp_id and old_corp_id != corp_id:
            raise HTTPException(
                status_code=400,
                detail="企业变更不允许直接修改，请删除后重新创建绑定",
            )
        if old_bot_id and old_bot_id != bot_id:
            raise HTTPException(
                status_code=400,
                detail="机器人变更不允许直接修改，请删除后重新创建绑定",
            )
        config = dict(old_config)
        config.update(
            {
                "bot_id": bot_id,
                "corp_id": corp_id,
                "bound_at": utc_now().isoformat(),
            }
        )
        account_key = external_account_key(binding.channel, config)
        if not account_key:
            raise HTTPException(status_code=400, detail="机器人 ID 无效")
        legacy_account_keys = legacy_external_account_keys(binding.channel, config)
        _ensure_external_account_available(
            db, account_key, binding.id, aliases=legacy_account_keys
        )
        db.rollback()
        _quiesce_binding_or_409(channel, binding_id, should_run=should_run)
        try:
            binding = _get_binding(db, request.tenant_id, binding_id)
            _ensure_revision(binding, expected_revision)
            current_config = dict(binding.config_json or {})
            if str(current_config.get("bot_id") or "").strip() != old_bot_id:
                raise HTTPException(status_code=409, detail="渠道配置已被其他请求修改，请重试")
            if str(current_config.get("corp_id") or "").strip() != old_corp_id:
                raise HTTPException(status_code=409, detail="渠道配置已被其他请求修改，请重试")
            _ensure_external_account_available(
                db, account_key, binding_id, aliases=legacy_account_keys
            )
            binding.credentials_enc = encrypt_channel_secret(secret)
            binding.config_json = config
            binding.external_account_key = account_key
            binding.identity_scope_key = corp_id
            binding.config_revision += 1
            binding.status = "active"
            binding.connected = False
            binding.updated_at = utc_now()
            db.add(binding)
            if old_bot_id and not old_corp_id and old_scope != corp_id:
                migrate_scope_for_binding(db, binding, old_scope, corp_id)
                migrate_binding_session_account_key(
                    db,
                    binding_id,
                    old_account_key,
                    account_key,
                )
            adopt_orphan_channel_sessions(db, binding)
            db.commit()
            db.refresh(binding)
        except IdentityScopeConflict as exc:
            db.rollback()
            _resume_binding(channel, binding_id, start=should_run)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IntegrityError as exc:
            db.rollback()
            _resume_binding(channel, binding_id, start=should_run)
            raise HTTPException(status_code=409, detail="该外部机器人已被其他渠道绑定使用") from exc
        except Exception:
            db.rollback()
            _resume_binding(channel, binding_id, start=should_run)
            raise
        _resume_binding(channel, binding_id, start=True)
    return channel_binding_read(db, binding)


@router.post("/{binding_id}/feishu/credentials", response_model=ChannelBindingRead)
def save_feishu_credentials(
    binding_id: str,
    request: FeishuCredentialsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelBindingRead:
    """Validate and save Feishu app credentials, then start its long connection."""
    ensure_current_user_tenant(request.tenant_id, current_user)
    binding = _get_binding(db, request.tenant_id, binding_id)
    _ensure_binding_manager(db, request.tenant_id, binding, current_user)
    if binding.channel != "feishu":
        raise HTTPException(status_code=400, detail="该绑定不是飞书渠道")
    app_id = request.app_id.strip()
    app_secret = request.app_secret.strip()
    if not app_id or not app_secret:
        raise HTTPException(status_code=400, detail="App ID 与 App Secret 均不能为空")
    old_app_id = str((binding.config_json or {}).get("app_id") or "").strip()
    if old_app_id and old_app_id != app_id:
        raise HTTPException(status_code=400, detail="应用变更不允许直接修改，请删除后重新创建绑定")
    try:
        bot_info = validate_feishu_credentials(app_id, app_secret)
    except FeishuPermanentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("验证飞书凭证失败 binding=%s", binding_id, exc_info=True)
        raise HTTPException(status_code=502, detail="飞书凭证验证暂时失败，请稍后重试") from exc

    account_key = external_account_key("feishu", {"app_id": app_id})
    if not account_key:
        raise HTTPException(status_code=400, detail="App ID 无效")
    _ensure_external_account_available(db, account_key, binding_id)
    db.rollback()
    with binding_lifecycle_lock(binding_id):
        binding = _get_binding(db, request.tenant_id, binding_id)
        expected_revision = binding.config_revision
        channel = binding.channel
        should_run = bool(binding.status == "active" and binding.credentials_enc)
        current_app_id = str((binding.config_json or {}).get("app_id") or "").strip()
        if current_app_id and current_app_id != app_id:
            raise HTTPException(status_code=409, detail="渠道配置已被其他请求修改，请重试")
        db.rollback()
        _quiesce_binding_or_409(channel, binding_id, should_run=should_run)
        try:
            binding = _get_binding(db, request.tenant_id, binding_id)
            _ensure_revision(binding, expected_revision)
            latest_app_id = str((binding.config_json or {}).get("app_id") or "").strip()
            if latest_app_id and latest_app_id != app_id:
                raise HTTPException(status_code=409, detail="渠道配置已被其他请求修改，请重试")
            _ensure_external_account_available(db, account_key, binding_id)
            config = dict(binding.config_json or {})
            config.update(
                {
                    "app_id": app_id,
                    "bot_open_id": bot_info["bot_open_id"],
                    "bot_name": bot_info.get("bot_name", ""),
                    "bound_at": utc_now().isoformat(),
                }
            )
            binding.credentials_enc = encrypt_channel_secret(app_secret)
            binding.config_json = config
            binding.external_account_key = account_key
            binding.config_revision += 1
            binding.status = "active"
            binding.connected = False
            binding.updated_at = utc_now()
            db.add(binding)
            adopt_orphan_channel_sessions(db, binding)
            db.commit()
            db.refresh(binding)
        except IntegrityError as exc:
            db.rollback()
            _resume_binding(channel, binding_id, start=should_run)
            raise HTTPException(status_code=409, detail="该飞书应用已被其他渠道绑定使用") from exc
        except Exception:
            db.rollback()
            _resume_binding(channel, binding_id, start=should_run)
            raise
        _resume_binding(channel, binding_id, start=True)
    return channel_binding_read(db, binding)


@router.post("/{binding_id}/dingtalk/credentials", response_model=ChannelBindingRead)
def save_dingtalk_credentials(
    binding_id: str,
    request: DingTalkCredentialsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelBindingRead:
    """Validate and save DingTalk Stream credentials, then start its connector."""
    ensure_current_user_tenant(request.tenant_id, current_user)
    binding = _get_binding(db, request.tenant_id, binding_id)
    _ensure_binding_manager(db, request.tenant_id, binding, current_user)
    if binding.channel != "dingtalk":
        raise HTTPException(status_code=400, detail="该绑定不是钉钉渠道")
    client_id = request.client_id.strip()
    client_secret = request.client_secret.strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="Client ID 与 Client Secret 均不能为空")
    old_client_id = str((binding.config_json or {}).get("client_id") or "").strip()
    if old_client_id and old_client_id != client_id:
        raise HTTPException(status_code=400, detail="应用变更不允许直接修改，请删除后重新创建绑定")
    try:
        validate_dingtalk_credentials(client_id, client_secret)
    except DingTalkPermanentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("验证钉钉凭证失败 binding=%s", binding_id, exc_info=True)
        raise HTTPException(status_code=502, detail="钉钉凭证验证暂时失败，请稍后重试") from exc
    account_key = external_account_key("dingtalk", {"client_id": client_id})
    if not account_key:
        raise HTTPException(status_code=400, detail="Client ID 无效")
    _ensure_external_account_available(db, account_key, binding_id)
    db.rollback()
    with binding_lifecycle_lock(binding_id):
        binding = _get_binding(db, request.tenant_id, binding_id)
        expected_revision = binding.config_revision
        should_run = bool(binding.status == "active" and binding.credentials_enc)
        current_client_id = str((binding.config_json or {}).get("client_id") or "").strip()
        if current_client_id and current_client_id != client_id:
            raise HTTPException(status_code=409, detail="渠道配置已被其他请求修改，请重试")
        db.rollback()
        _quiesce_binding_or_409(binding.channel, binding_id, should_run=should_run)
        try:
            binding = _get_binding(db, request.tenant_id, binding_id)
            _ensure_revision(binding, expected_revision)
            latest_client_id = str((binding.config_json or {}).get("client_id") or "").strip()
            if latest_client_id and latest_client_id != client_id:
                raise HTTPException(status_code=409, detail="渠道配置已被其他请求修改，请重试")
            _ensure_external_account_available(db, account_key, binding_id)
            config = dict(binding.config_json or {})
            config.update({"client_id": client_id, "bot_name": "钉钉机器人", "bound_at": utc_now().isoformat()})
            binding.credentials_enc = encrypt_channel_secret(client_secret)
            binding.config_json = config
            binding.external_account_key = account_key
            binding.config_revision += 1
            binding.status = "active"
            binding.connected = False
            binding.updated_at = utc_now()
            db.add(binding)
            adopt_orphan_channel_sessions(db, binding)
            db.commit()
            db.refresh(binding)
        except IntegrityError as exc:
            db.rollback()
            _resume_binding(binding.channel, binding_id, start=should_run)
            raise HTTPException(status_code=409, detail="该钉钉应用已被其他渠道绑定使用") from exc
        except Exception:
            db.rollback()
            _resume_binding(binding.channel, binding_id, start=should_run)
            raise
        _resume_binding(binding.channel, binding_id, start=True)
    return channel_binding_read(db, binding)


@router.post("/{binding_id}/discord/credentials", response_model=ChannelBindingRead)
def save_discord_credentials(
    binding_id: str,
    request: DiscordCredentialsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelBindingRead:
    """Validate and save Discord Bot Token, then start its connector."""
    ensure_current_user_tenant(request.tenant_id, current_user)
    binding = _get_binding(db, request.tenant_id, binding_id)
    _ensure_binding_manager(db, request.tenant_id, binding, current_user)
    if binding.channel != "discord":
        raise HTTPException(status_code=400, detail="该绑定不是 Discord 渠道")
    bot_token = request.bot_token.strip()
    if not bot_token:
        raise HTTPException(status_code=400, detail="Bot Token 不能为空")
    old_bot_id = str((binding.config_json or {}).get("bot_id") or "").strip()
    try:
        bot_info = validate_discord_credentials(bot_token)
    except DiscordPermanentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("验证 Discord 凭证失败 binding=%s", binding_id, exc_info=True)
        raise HTTPException(status_code=502, detail="Discord 凭证验证暂时失败，请稍后重试") from exc
    bot_id = str(bot_info.get("bot_id") or "").strip()
    bot_name = str(bot_info.get("bot_name") or "").strip()
    if not bot_id:
        raise HTTPException(status_code=400, detail="Bot 信息无效")
    if old_bot_id and old_bot_id != bot_id:
        raise HTTPException(status_code=400, detail="应用变更不允许直接修改，请删除后重新创建绑定")
    account_key = external_account_key("discord", {"bot_id": bot_id})
    if not account_key:
        raise HTTPException(status_code=400, detail="Bot 信息无效")
    _ensure_external_account_available(db, account_key, binding_id)
    db.rollback()
    with binding_lifecycle_lock(binding_id):
        binding = _get_binding(db, request.tenant_id, binding_id)
        expected_revision = binding.config_revision
        should_run = bool(binding.status == "active" and binding.credentials_enc)
        current_bot_id = str((binding.config_json or {}).get("bot_id") or "").strip()
        if current_bot_id and current_bot_id != bot_id:
            raise HTTPException(status_code=409, detail="渠道配置已被其他请求修改，请重试")
        db.rollback()
        _quiesce_binding_or_409(binding.channel, binding_id, should_run=should_run)
        try:
            binding = _get_binding(db, request.tenant_id, binding_id)
            _ensure_revision(binding, expected_revision)
            latest_bot_id = str((binding.config_json or {}).get("bot_id") or "").strip()
            if latest_bot_id and latest_bot_id != bot_id:
                raise HTTPException(status_code=409, detail="渠道配置已被其他请求修改，请重试")
            _ensure_external_account_available(db, account_key, binding_id)
            config = dict(binding.config_json or {})
            config.update({"bot_id": bot_id, "bot_name": bot_name, "bound_at": utc_now().isoformat()})
            binding.credentials_enc = encrypt_channel_secret(bot_token)
            binding.config_json = config
            binding.external_account_key = account_key
            binding.config_revision += 1
            binding.status = "active"
            binding.connected = False
            binding.updated_at = utc_now()
            db.add(binding)
            adopt_orphan_channel_sessions(db, binding)
            db.commit()
            db.refresh(binding)
        except IntegrityError as exc:
            db.rollback()
            _resume_binding(binding.channel, binding_id, start=should_run)
            raise HTTPException(status_code=409, detail="该 Discord 机器人已被其他渠道绑定使用") from exc
        except Exception:
            db.rollback()
            _resume_binding(binding.channel, binding_id, start=should_run)
            raise
        _resume_binding(binding.channel, binding_id, start=True)
    return channel_binding_read(db, binding)


def _batch_target_for(binding: ChannelBinding, channel_id: str, thread_id: str | None) -> dict[str, Any]:
    """构造批量投递目标:channel_id 定位 REST 出站,thread_id 走线程(功能2)。"""
    target: dict[str, Any] = {
        "to_user_id": "",
        "channel_id": channel_id,
        "guild_id": str((binding.config_json or {}).get("guild_id") or ""),
    }
    if thread_id:
        target["thread_id"] = thread_id
    return target


def _binding_channel_id(binding: ChannelBinding, config_key: str) -> str:
    """批量/回填默认目标频道:请求显式传入优先,否则回退 config_json 段。"""
    config = binding.config_json or {}
    section = config.get(config_key)
    if isinstance(section, dict):
        return str(section.get("channel_id") or "").strip()
    return ""


@router.post("/{binding_id}/batch", response_model=BatchSubmitRead)
def submit_channel_batch(
    binding_id: str,
    request: BatchSendRequest,
    tenant_id: str = Query(...),
    channel_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BatchSubmitRead:
    """提交批量投递作业(功能3):items 逐条限流整形发送,返回 job_id 供进度轮询。"""
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    target_channel_id = (channel_id or "").strip() or _binding_channel_id(binding, "batch")
    if not target_channel_id:
        raise HTTPException(status_code=400, detail="缺少目标频道 ID(channel_id 或 config_json.batch.channel_id)")
    target = _batch_target_for(binding, target_channel_id, request.thread_id)
    job_id = batch_service.submit_batch(
        binding,
        tenant_id,
        target,
        [item if isinstance(item, str) else item.model_dump() for item in request.items],
        thread_id=request.thread_id,
        client_batch_id=request.client_batch_id,
    )
    job = batch_service.get_batch(job_id)
    return BatchSubmitRead(job_id=job_id, status=job.status if job else "pending")


@router.get("/{binding_id}/batch/{job_id}", response_model=BatchJobRead)
def get_channel_batch_job(
    binding_id: str,
    job_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BatchJobRead:
    """查询批量作业进度(功能3):status/progress/succeeded/failed/errors。"""
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    job = batch_service.get_batch(job_id)
    if job is None or job.binding_id != binding_id:
        raise HTTPException(status_code=404, detail="批量作业不存在")
    return BatchJobRead(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        total=len(job.items),
        succeeded=job.succeeded,
        failed=job.failed,
        errors=list(job.errors),
    )


@router.post("/{binding_id}/backfill", response_model=BackfillJobRead)
def submit_channel_backfill(
    binding_id: str,
    request: BackfillRequest,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BackfillJobRead:
    """手动触发历史回填(功能4):拉取频道历史写入 backfilled 事件,不触发 agent。

    自动回填(绑定激活时按 config_json.features.backfill 触发)为后续演进,
    首版仅提供管理员手动触发端点。
    """
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    if binding.channel != "discord":
        raise HTTPException(status_code=400, detail="该绑定不是 Discord 渠道")
    target_channel_id = (request.channel_id or "").strip() or _binding_channel_id(binding, "backfill")
    if not target_channel_id:
        raise HTTPException(status_code=400, detail="缺少目标频道 ID(channel_id 或 config_json.backfill.channel_id)")
    config = binding.config_json or {}
    backfill_config = config.get("backfill")
    limit = request.limit or (
        int(backfill_config.get("limit_per_fetch", 100)) if isinstance(backfill_config, dict) else 100
    )
    job_id = batch_service.submit_backfill(
        binding,
        channel_id=target_channel_id,
        limit=min(limit, 100),
        after=request.after,
        before=request.before,
    )
    job = batch_service.get_backfill(job_id)
    return BackfillJobRead(
        job_id=job_id,
        status=job.status if job else "pending",
        written=job.written if job else 0,
        duplicates=job.duplicates if job else 0,
        errors=list(job.errors) if job else [],
    )


@router.get("/{binding_id}/backfill/{job_id}", response_model=BackfillJobRead)
def get_channel_backfill_job(
    binding_id: str,
    job_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BackfillJobRead:
    """查询回填作业进度(功能4)。"""
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    job = batch_service.get_backfill(job_id)
    if job is None or job.binding_id != binding_id:
        raise HTTPException(status_code=404, detail="回填作业不存在")
    return BackfillJobRead(
        job_id=job.job_id,
        status=job.status,
        written=job.written,
        duplicates=job.duplicates,
        errors=list(job.errors),
    )


@router.get("/delivery-audit", response_model=ChannelDeliveryPage)
def list_tenant_delivery_audit(
    tenant_id: str = Query(...),
    binding_id: str | None = Query(None),
    session_id: str | None = Query(None),
    status: str | None = Query("failed"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelDeliveryPage:
    """Tenant-admin audit that remains available after a binding is deleted."""
    ensure_current_user_tenant(tenant_id, current_user)
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only administrators can audit channel deliveries")
    from sqlalchemy import func

    filters = [ChannelDelivery.tenant_id == tenant_id]
    if binding_id:
        filters.append(ChannelDelivery.binding_id == binding_id)
    if session_id:
        filters.append(ChannelDelivery.session_id == session_id)
    if status:
        if status not in {"pending", "sending", "delivered", "failed"}:
            raise HTTPException(status_code=400, detail="Invalid delivery status")
        filters.append(ChannelDelivery.status == status)
    total = db.exec(
        select(func.count()).select_from(ChannelDelivery).where(*filters)
    ).one()
    rows = db.exec(
        select(ChannelDelivery)
        .where(*filters)
        .order_by(ChannelDelivery.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return ChannelDeliveryPage(
        items=[channel_delivery_read(row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/{binding_id}/deliveries", response_model=ChannelDeliveryPage)
def list_channel_deliveries(
    binding_id: str,
    tenant_id: str = Query(...),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelDeliveryPage:
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    from sqlalchemy import func

    total = db.exec(
        select(func.count()).select_from(ChannelDelivery).where(
            ChannelDelivery.binding_id == binding.id
        )
    ).one()
    rows = db.exec(
        select(ChannelDelivery)
        .where(ChannelDelivery.binding_id == binding.id)
        .order_by(ChannelDelivery.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return ChannelDeliveryPage(
        items=[channel_delivery_read(row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/{binding_id}/deliveries/days", response_model=ChannelDeliveryDayPage)
def list_channel_delivery_days(
    binding_id: str,
    tenant_id: str = Query(...),
    offset: int = Query(0, ge=0),
    limit: int = Query(7, le=30),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelDeliveryDayPage:
    """投递日志按天分组分页:整天为单位翻页,命中天的记录全天返回不截断。"""
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    from sqlalchemy import func

    # 按服务器本地时区的自然日分桶(SQLite date(created_at, 'localtime'))
    day_bucket = func.date(ChannelDelivery.created_at, "localtime")
    day_rows = db.exec(
        select(day_bucket, func.count())
        .where(ChannelDelivery.binding_id == binding.id)
        .group_by(day_bucket)
        .order_by(day_bucket.desc())
    ).all()
    total_days = len(day_rows)
    days: list[ChannelDeliveryDay] = []
    for day_value, _count in day_rows[offset : offset + limit]:
        rows = db.exec(
            select(ChannelDelivery)
            .where(ChannelDelivery.binding_id == binding.id, day_bucket == day_value)
            .order_by(ChannelDelivery.created_at.desc())
        ).all()
        days.append(
            ChannelDeliveryDay(
                date=str(day_value),
                count=len(rows),
                items=[channel_delivery_read(row) for row in rows],
            )
        )
    return ChannelDeliveryDayPage(days=days, total_days=total_days, offset=offset, limit=limit)


def _binding_channel_sessions(db: Session, binding: ChannelBinding) -> list[ChatSession]:
    """该绑定的渠道会话:直挂 channel_binding_id 的 + legacy 兜底(v1.1 前未写 binding_id)。"""
    from app.channels.service_routing import mounted_agents

    agent_ids = [mount.agent_id for mount in mounted_agents(db, binding)]
    direct = db.exec(
        select(ChatSession).where(
            ChatSession.tenant_id == binding.tenant_id,
            ChatSession.channel_binding_id == binding.id,
        )
    ).all()
    legacy = db.exec(
        select(ChatSession).where(
            ChatSession.tenant_id == binding.tenant_id,
            ChatSession.channel_binding_id.is_(None),
            ChatSession.channel == binding.channel,
            ChatSession.external_conv_id.is_not(None),
            ChatSession.agent_id.in_(agent_ids),
        )
    ).all()
    sessions: dict[str, ChatSession] = {}
    for row in [*direct, *legacy]:
        sessions[row.id] = row
    return list(sessions.values())


@router.get("/{binding_id}/conversations", response_model=ChannelConversationPage)
def list_channel_conversations(
    binding_id: str,
    tenant_id: str = Query(...),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelConversationPage:
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    sessions = _binding_channel_sessions(db, binding)
    sessions.sort(key=lambda row: row.updated_at, reverse=True)
    total = len(sessions)
    page = sessions[offset : offset + limit]

    session_ids = [row.id for row in page]
    user_ids = [row.user_id for row in page if row.user_id]
    agent_ids = [row.agent_id for row in page if row.agent_id]
    identity_names: dict[str, str] = {}
    user_names: dict[str, str] = {}
    agent_name_map: dict[str, str] = {}
    message_counts: dict[str, int] = {}
    if user_ids:
        identities = db.exec(
            select(ChannelIdentity).where(ChannelIdentity.staffdeck_user_id.in_(user_ids))
        ).all()
        identity_names = {
            row.staffdeck_user_id: row.display_name for row in identities if row.display_name
        }
        users = db.exec(select(User).where(User.id.in_(user_ids))).all()
        user_names = {row.id: row.display_name for row in users if row.display_name}
    if agent_ids:
        agents = db.exec(select(AgentProfile).where(AgentProfile.id.in_(agent_ids))).all()
        agent_name_map = {row.id: row.name for row in agents}
    if session_ids:
        from sqlalchemy import func

        count_rows = db.exec(
            select(Message.session_id, func.count())
            .where(Message.session_id.in_(session_ids))
            .group_by(Message.session_id)
        ).all()
        message_counts = {session_id: count for session_id, count in count_rows}

    group_prefix = f"{binding.channel}_group_"
    conversations: list[ChannelConversationRead] = []
    for chat_session in page:
        last_message = db.exec(
            select(Message)
            .where(Message.session_id == chat_session.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        ).first()
        external_conv_id = chat_session.external_conv_id
        conversations.append(
            ChannelConversationRead(
                session_id=chat_session.id,
                external_conv_id=external_conv_id,
                display_name=identity_names.get(chat_session.user_id)
                or user_names.get(chat_session.user_id),
                is_group=bool(external_conv_id and external_conv_id.startswith(group_prefix)),
                agent_id=chat_session.agent_id,
                agent_name=agent_name_map.get(chat_session.agent_id),
                message_count=message_counts.get(chat_session.id, 0),
                last_message_preview=(last_message.content or "")[:60] if last_message else None,
                updated_at=chat_session.updated_at.isoformat(),
            )
        )
    return ChannelConversationPage(items=conversations, total=total, offset=offset, limit=limit)


@router.get(
    "/{binding_id}/conversations/{session_id}/messages",
    response_model=list[ChannelConversationMessageRead],
)
def list_channel_conversation_messages(
    binding_id: str,
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ChannelConversationMessageRead]:
    ensure_current_user_tenant(tenant_id, current_user)
    binding = _get_binding(db, tenant_id, binding_id)
    _ensure_binding_manager(db, tenant_id, binding, current_user)
    session_ids = {row.id for row in _binding_channel_sessions(db, binding)}
    if session_id not in session_ids:
        raise HTTPException(status_code=404, detail="Channel conversation not found")
    chat_session = db.get(ChatSession, session_id)
    rows = db.exec(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at)
    ).all()
    items = [
        ChannelConversationMessageRead(
            id=row.id,
            role=row.role,
            content=row.content,
            created_at=row.created_at.isoformat(),
            attachments=[
                ChannelConversationAttachmentRead(
                    id=item.get("id"),
                    filename=item.get("filename"),
                    content_type=item.get("content_type"),
                    size=item.get("size"),
                    kind=item.get("kind"),
                )
                for item in ((row.metadata_json or {}).get("attachments") or [])
            ]
            or None,
        )
        for row in rows
    ]
    # §4.4 回填可见性:合并同频道的 backfilled 入站事件(历史消息不触发 agent,
    # 不在 Message 表),按真实消息时间与对话消息统一排序。
    if chat_session is not None:
        items.extend(_backfilled_conversation_messages(db, binding, chat_session))
    items.sort(key=lambda item: _message_sort_key(item.created_at))
    return items[:200]


def _backfill_channel_key(external_conv_id: str | None) -> str | None:
    """从会话 external_conv_id 反推回填频道键。

    Discord 群聊会话形如 `discord_group_{conv_key}`,conv_key 与回填事件
    target_json 的 guild_id/channel_id 对应;私聊与未知格式返回 None(不合并)。
    """
    marker = "_group_"
    if not external_conv_id:
        return None
    index = external_conv_id.rfind(marker)
    if index == -1:
        return None
    return external_conv_id[index + len(marker):] or None


def _backfill_event_matches_channel(event: ChannelInboundEvent, channel_key: str) -> bool:
    target = event.target_json
    if not isinstance(target, dict):
        return False
    return channel_key in {
        str(target.get("channel_id") or ""),
        str(target.get("guild_id") or ""),
    }


def _message_sort_key(created_at: str) -> datetime:
    """对话消息与回填消息的统一排序键;时间戳不可解析时兜底到最早。

    Message.created_at(naive UTC)与回填事件里的 Discord ISO 时间戳(带 +00:00
    偏移)混排,统一补 UTC 时区后再比较。
    """
    try:
        parsed = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return datetime.min
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _backfilled_conversation_messages(
    db: Session,
    binding: ChannelBinding,
    chat_session: ChatSession,
) -> list[ChannelConversationMessageRead]:
    """会话同频道的 backfilled 事件 → web 消息视图(方案 A,§4.4)。"""
    channel_key = _backfill_channel_key(chat_session.external_conv_id)
    if channel_key is None:
        return []
    events = db.exec(
        select(ChannelInboundEvent).where(
            ChannelInboundEvent.binding_id == binding.id,
            ChannelInboundEvent.status == "backfilled",
        )
    ).all()
    bot_id = str((binding.config_json or {}).get("bot_id") or "")
    result: list[ChannelConversationMessageRead] = []
    for event in events:
        if not _backfill_event_matches_channel(event, channel_key):
            continue
        message = event.payload_json.get("message") or {}
        if not isinstance(message, dict):
            continue
        content = str(message.get("content") or "").strip()
        author_id = str(message.get("author_id") or "")
        role = "assistant" if author_id and author_id == bot_id else "user"
        created_at = str(message.get("created_at") or "").strip()
        if not created_at:
            created_at = event.created_at.isoformat()
        result.append(
            ChannelConversationMessageRead(
                id=str(event.event_id or event.id),
                role=role,
                content=content,
                created_at=created_at,
                attachments=[
                    ChannelConversationAttachmentRead(
                        id=item.get("id"),
                        filename=item.get("filename"),
                        content_type=item.get("content_type"),
                        size=item.get("size"),
                        kind=item.get("kind"),
                    )
                    for item in (message.get("attachments") or [])
                    if isinstance(item, dict)
                ]
                or None,
            )
        )
    return result
