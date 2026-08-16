from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from app.db.models import ChannelBinding, ChannelDelivery, User


def _validate_allowlist_id(value: str) -> str:
    """白名单 ID 宽松校验:Discord snowflake 为数字字符串,允许非空即可。

    首版不做严格格式约束,便于粘贴 guild/channel/role/user ID;
    非法值由渠道侧 fence 判定时自然不匹配,不会产生错误放行。
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("白名单 ID 不能为空")
    return stripped


class AllowlistConfig(BaseModel):
    """权限白名单配置(功能5,§4.5):入站侧访问控制,出站投递不拦截。

    mode=allow_all 时 allow 列表为空即放行、非空则仅列表内放行(黑名单增强);
    mode=deny_all 时仅 allow 列表内放行(白名单严格模式);deny 永远优先。
    """

    mode: Literal["allow_all", "deny_all"] = "allow_all"
    guild_ids: list[str] = Field(default_factory=list)
    channel_ids: list[str] = Field(default_factory=list)
    role_ids: list[str] = Field(default_factory=list)
    user_ids: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    @field_validator(
        "guild_ids", "channel_ids", "role_ids", "user_ids", "deny"
    )
    @classmethod
    def _validate_ids(cls, values: list[str]) -> list[str]:
        return [_validate_allowlist_id(value) for value in values]


class ChannelFeaturesConfig(BaseModel):
    """能力开关(§3.1 features):运行时门禁的显式开关,与适配器能力声明正交。"""

    slash_commands: bool = True
    threads: bool = True
    # 自动建线程默认关闭,保存存量行为(未开启时不在新会话自动建线程)
    auto_thread: bool = False
    typing: bool = True
    voice: bool = False
    rich_media: bool = True
    backfill: bool = True


class ChannelBatchConfig(BaseModel):
    """批处理队列参数(§3.1 batch,功能3):限流整形与单次作业上限。"""

    max_per_run: int = Field(default=20, ge=1)
    interval_sec: float = Field(default=1.0, gt=0)


class ChannelBackfillConfig(BaseModel):
    """回填参数(§3.1 backfill,功能4):单次拉取上限与历史窗口。"""

    limit_per_fetch: int = Field(default=100, ge=1, le=100)
    max_history_days: int = Field(default=7, ge=1)


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
    # 权限白名单(功能5):不传不动,传则写 config_json.allowlist 且 config_revision+=1
    allowlist: Optional[AllowlistConfig] = None
    # 能力开关(§3.1 features):不传不动,传则写 config_json.features 且 config_revision+=1
    features: Optional[ChannelFeaturesConfig] = None


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
    app_id: Optional[str] = None
    client_id: Optional[str] = None
    bot_open_id: Optional[str] = None
    bot_name: Optional[str] = None
    provider_tenant_key: Optional[str] = None
    config_revision: int = 0
    session_expired: bool = False
    bound_at: Optional[str] = None
    created_by_user_id: Optional[str] = None
    created_by_name: Optional[str] = None
    agents: list[ChannelBindingAgentRead] = []
    auto_route: bool = True
    config_json: Optional[dict] = None
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


class FeishuCredentialsRequest(BaseModel):
    tenant_id: str
    app_id: str
    app_secret: str


class DingTalkCredentialsRequest(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: str


class DiscordCredentialsRequest(BaseModel):
    tenant_id: str
    bot_token: str


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
    payload_json: Optional[str] = None
    thread_id: Optional[str] = None
    batch_id: Optional[str] = None
    delivery_kind: Optional[str] = None
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


class ChannelConversationAttachmentRead(BaseModel):
    """会话消息附件的精简元数据视图。

    仅暴露展示所需的字段，避免把 data_url(base64)、sandbox_path 等
    内部字段或大体量内容塞进会话列表响应。
    """

    id: Optional[str] = None
    filename: Optional[str] = None
    content_type: Optional[str] = None
    size: Optional[int] = None
    kind: Optional[str] = None


class ChannelConversationMessageRead(BaseModel):
    id: str
    role: str
    content: str
    created_at: str
    attachments: list[ChannelConversationAttachmentRead] | None = None


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


# ---------- 批处理作业(功能3,§4.3) ----------


class BatchSendItem(BaseModel):
    """批量投递单条消息:纯文本(content)或富媒体载荷(embeds/files 非空)。"""

    content: str = ""
    embeds: list[dict[str, Any]] = Field(default_factory=list)
    files: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_rich(self) -> bool:
        return bool(self.embeds or self.files)


class BatchSendRequest(BaseModel):
    """POST /batch 请求体:items 兼容纯文本列表或结构化载荷列表。"""

    items: list[Union[str, BatchSendItem]]
    thread_id: Optional[str] = None
    # 客户端幂等 ID:重复提交返回既有作业
    client_batch_id: Optional[str] = None

    @field_validator("items")
    @classmethod
    def _validate_items(cls, items: list[Union[str, BatchSendItem]]) -> list[Union[str, BatchSendItem]]:
        if not items:
            raise ValueError("items 不能为空")
        return items


class BatchJobRead(BaseModel):
    """批量作业查询视图(内存首版,无 DB 持久化)。"""

    job_id: str
    status: str
    progress: int
    total: int
    succeeded: int
    failed: int
    errors: list[str] = []


class BatchSubmitRead(BaseModel):
    job_id: str
    status: str


# ---------- 历史回填(功能4,§4.4) ----------


class BackfillRequest(BaseModel):
    channel_id: Optional[str] = None
    limit: Optional[int] = Field(default=None, ge=1, le=100)
    after: Optional[str] = None
    before: Optional[str] = None


class BackfillJobRead(BaseModel):
    job_id: str
    status: str
    written: int = 0
    duplicates: int = 0
    errors: list[str] = []


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


def channel_binding_creator_name(db: Session, binding: ChannelBinding) -> Optional[str]:
    """创建者展示名;用户已删除或存量绑定无 created_by_user_id 时返回 None。"""
    if not binding.created_by_user_id:
        return None
    user = db.get(User, binding.created_by_user_id)
    if not user:
        return None
    return user.display_name or user.username


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
        app_id=config.get("app_id"),
        client_id=config.get("client_id"),
        bot_open_id=config.get("bot_open_id"),
        bot_name=config.get("bot_name"),
        provider_tenant_key=binding.provider_tenant_key,
        config_revision=binding.config_revision,
        session_expired=bool(config.get("session_expired")),
        bound_at=str(bound_at) if bound_at else None,
        created_by_user_id=binding.created_by_user_id,
        created_by_name=channel_binding_creator_name(db, binding),
        agents=channel_binding_agents_read(db, binding),
        auto_route=(binding.config_json or {}).get("auto_route") is not False,
        config_json=binding.config_json,
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
        payload_json=delivery.payload_json,
        thread_id=delivery.thread_id,
        batch_id=delivery.batch_id,
        delivery_kind=delivery.delivery_kind,
        status=delivery.status,
        attempts=delivery.attempts,
        last_error=delivery.last_error,
        delivered_at=delivery.delivered_at.isoformat() if delivery.delivered_at else None,
        created_at=delivery.created_at.isoformat(),
    )
