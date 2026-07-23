from __future__ import annotations

from typing import Optional

from pydantic import BaseModel
from sqlmodel import Session

from app.db.models import ChannelBinding, ChannelDelivery


class ChannelBindingCreate(BaseModel):
    tenant_id: str
    agent_id: str
    channel: str = "wechat"


class ChannelBindingAgentRead(BaseModel):
    agent_id: str
    name: Optional[str] = None
    is_default: bool = False


class ChannelBindingAgentInput(BaseModel):
    agent_id: str
    is_default: bool = False


class ChannelBindingAgentsUpdate(BaseModel):
    # 为 None 时跳过挂载集替换(仅更新开关);为 [] 时报 400 不允许空列表
    agents: Optional[list[ChannelBindingAgentInput]] = None
    # 智能分发开关:不传不动,传则写 config_json.auto_route
    auto_route: Optional[bool] = None


class ChannelBindingRead(BaseModel):
    """绑定信息对外视图：只暴露配置元数据，绝不回传凭证明文。"""

    id: str
    tenant_id: str
    agent_id: str
    channel: str
    status: str
    connected: bool
    ilink_bot_id: Optional[str] = None
    baseurl: Optional[str] = None
    bot_id: Optional[str] = None
    corp_id: Optional[str] = None
    config_revision: int = 0
    session_expired: bool = False
    bound_at: Optional[str] = None
    created_by_user_id: Optional[str] = None
    agents: list[ChannelBindingAgentRead] = []
    auto_route: bool = True
    created_at: str
    updated_at: str


class ChannelQRCodeRead(BaseModel):
    qrcode: str
    qrcode_img_content: Optional[str] = None


class ChannelQRCodeStatusRead(BaseModel):
    status: str
    binding: Optional[ChannelBindingRead] = None


class ChannelBindCodeRead(BaseModel):
    code: str
    expires_at: str


class MyIdentityBindingRead(BaseModel):
    channel: str
    external_user_id: str
    display_name: Optional[str] = None
    bound_at: str
    # 渠道账号作用域(前端 scope 标签;wechat 为空串)
    external_account_scope: str = ""


class WeComCredentialsRequest(BaseModel):
    tenant_id: str
    bot_id: str
    secret: str
    # 企业 ID 是企微 userid 的真实唯一边界,首次激活即必须提供
    corp_id: str


class ChannelCredentialFieldRead(BaseModel):
    key: str
    label: str
    placeholder: Optional[str] = None
    secret: bool = False
    optional: bool = False


class ChannelMetaRead(BaseModel):
    channel: str
    name: str
    setup: str
    credential_fields: list[ChannelCredentialFieldRead] = []
    capabilities: list[str] = []


class ChannelDeliveryRead(BaseModel):
    id: str
    binding_id: str
    session_id: str
    message_id: Optional[str] = None
    kind: str
    text: str
    status: str
    attempts: int
    last_error: Optional[str] = None
    delivered_at: Optional[str] = None
    created_at: str


class ChannelConversationRead(BaseModel):
    session_id: str
    external_conv_id: Optional[str] = None
    display_name: Optional[str] = None
    is_group: bool = False
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    message_count: int = 0
    last_message_preview: Optional[str] = None
    updated_at: str


class ChannelConversationMessageRead(BaseModel):
    id: str
    role: str
    content: str
    created_at: str


class ChannelConversationPage(BaseModel):
    items: list[ChannelConversationRead]
    total: int
    offset: int
    limit: int


class ChannelDeliveryPage(BaseModel):
    items: list[ChannelDeliveryRead]
    total: int
    offset: int
    limit: int


class ChannelDeliveryDay(BaseModel):
    date: str
    count: int
    items: list[ChannelDeliveryRead]


class ChannelDeliveryDayPage(BaseModel):
    days: list[ChannelDeliveryDay]
    total_days: int
    offset: int
    limit: int


def channel_binding_agents_read(db: Session, binding: ChannelBinding) -> list[ChannelBindingAgentRead]:
    """挂载员工列表(含存量绑定 legacy 回退),join agent_profiles 取名称。"""
    from app.channels.service_routing import agent_names, mounted_agents

    mounts = mounted_agents(db, binding)
    names = agent_names(db, binding.tenant_id, [mount.agent_id for mount in mounts])
    return [
        ChannelBindingAgentRead(
            agent_id=mount.agent_id,
            name=names.get(mount.agent_id),
            is_default=mount.is_default,
        )
        for mount in mounts
    ]


def channel_binding_read(db: Session, binding: ChannelBinding) -> ChannelBindingRead:
    config = dict(binding.config_json or {})
    bound_at = config.get("bound_at")
    return ChannelBindingRead(
        id=binding.id,
        tenant_id=binding.tenant_id,
        agent_id=binding.agent_id,
        channel=binding.channel,
        status=binding.status,
        connected=binding.connected,
        ilink_bot_id=config.get("ilink_bot_id"),
        baseurl=config.get("baseurl"),
        bot_id=config.get("bot_id"),
        corp_id=config.get("corp_id"),
        config_revision=binding.config_revision,
        session_expired=bool(config.get("session_expired")),
        bound_at=str(bound_at) if bound_at else None,
        created_by_user_id=binding.created_by_user_id,
        agents=channel_binding_agents_read(db, binding),
        auto_route=(binding.config_json or {}).get("auto_route") is not False,
        created_at=binding.created_at.isoformat(),
        updated_at=binding.updated_at.isoformat(),
    )


def channel_delivery_read(delivery: ChannelDelivery) -> ChannelDeliveryRead:
    return ChannelDeliveryRead(
        id=delivery.id,
        binding_id=delivery.binding_id,
        session_id=delivery.session_id,
        message_id=delivery.message_id,
        kind=delivery.kind,
        text=delivery.text,
        status=delivery.status,
        attempts=delivery.attempts,
        last_error=delivery.last_error,
        delivered_at=delivery.delivered_at.isoformat() if delivery.delivered_at else None,
        created_at=delivery.created_at.isoformat(),
    )
