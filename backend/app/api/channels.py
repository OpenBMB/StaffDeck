from __future__ import annotations

import json
import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.channels import (
    binding_lifecycle_lock,
    channel_services_enabled,
    pause_binding_ingress,
    resume_binding_ingress,
    wait_binding_ingress_stopped,
)
from app.channels.adapters.wechat import WeChatClient, sanitize_wechat_baseurl, validate_wechat_host
from app.channels.crypto import decrypt_channel_secret, encrypt_channel_secret
from app.channels.schema import (
    ChannelBindingAgentRead,
    ChannelBindingAgentsUpdate,
    ChannelBindingCreate,
    ChannelBindingRead,
    ChannelBindCodeRead,
    ChannelConversationMessageRead,
    ChannelConversationPage,
    ChannelConversationRead,
    ChannelDeliveryDay,
    ChannelDeliveryDayPage,
    ChannelDeliveryPage,
    ChannelMetaRead,
    ChannelQRCodeRead,
    ChannelQRCodeStatusRead,
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
    ChannelBinding,
    ChannelBindingAgent,
    ChannelBindCode,
    ChannelConvState,
    ChannelDelivery,
    ChannelIdentity,
    ChatSession,
    Message,
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

SUPPORTED_CHANNELS = {"wechat", "wecom"}
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
    ensure_agent_scope_manager(db, request.tenant_id, request.agent_id, current_user)
    # 同一员工同一渠道允许多个绑定实例,总是新建
    binding = ChannelBinding(
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        channel=request.channel,
        status="pending",
        created_by_user_id=current_user.id,
    )
    db.add(binding)
    db.flush()
    # 新绑定自动挂载默认员工
    db.add(
        ChannelBindingAgent(
            tenant_id=request.tenant_id,
            binding_id=binding.id,
            agent_id=request.agent_id,
            is_default=True,
            sort_order=0,
        )
    )
    db.commit()
    db.refresh(binding)
    return channel_binding_read(db, binding)


BIND_CODE_TTL_MINUTES = 10


def _generate_bind_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


@router.post("/bind-code", response_model=ChannelBindCodeRead)
def create_bind_code(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChannelBindCodeRead:
    """为当前用户生成微信身份绑定码(6 位数字,10 分钟有效,旧码作废)。"""
    ensure_current_user_tenant(tenant_id, current_user)
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
        )
        for row in rows
    ]


@router.delete("/my-identity-bindings/{channel}", status_code=204)
def delete_my_identity_binding(
    channel: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> Response:
    """页面侧解除当前用户在指定渠道的身份绑定(效果同 /解绑 指令)。"""
    ensure_current_user_tenant(tenant_id, current_user)
    identities = db.exec(
        select(ChannelIdentity).where(
            ChannelIdentity.tenant_id == tenant_id,
            ChannelIdentity.channel == channel,
            ChannelIdentity.staffdeck_user_id == current_user.id,
        )
    ).all()
    if not identities:
        raise HTTPException(status_code=404, detail="Identity binding not found")
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
    if request.agents is None and request.auto_route is None:
        raise HTTPException(status_code=400, detail="无有效更新内容")
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
        try:
            binding = _get_binding(db, tenant_id, binding_id)
            _ensure_revision(binding, expected_revision)
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
    rows = db.exec(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at)
        .limit(200)
    ).all()
    return [
        ChannelConversationMessageRead(
            id=row.id,
            role=row.role,
            content=row.content,
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    ]
