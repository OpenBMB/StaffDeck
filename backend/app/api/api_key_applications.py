from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select

from app.aliyun_aigw import (
    AliyunApigError,
    get_apig_client,
    get_gateway_config,
    get_gateway_configs,
)
from app.aigw_cache import (
    acquire_sync_throttle,
    get_json,
    invalidate_tenant,
    set_json,
)
from app.config import get_settings
from app.db import get_session
from app.db.models import (
    ApiKeyApplication,
    ApiKeyConsumer,
    ApiKeyConsumerGroup,
    ApiKeyQuotaRule,
    ApiKeyUsageSnapshot,
    User,
    utc_now,
)
from app.security.auth import get_current_user
from app.security.encryption import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    try_decrypt_secret,
)
from app.security.permissions import ensure_tenant_admin
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/api-key-applications",
    tags=["enterprise:api-key-applications"],
    dependencies=[Depends(get_current_user)],
)

# ============ 写透模式辅助函数 ============


def _sync_consumer_groups_to_local(
    db: Session,
    tenant_id: str,
    client,
    gateways: list,
    mirror: bool = False,
) -> dict[str, int]:
    """从阿里云同步消费组到本地 DB（幂等）。

    mirror=False（GET 列表等隐式同步）：仅新增/更新，不删本地数据。
    mirror=True（手动同步按钮）：完整镜像语义，云端已删除的组同步移除
    （仅镜像行，无 external id 的纯本地记录保留）。
    返回变更计数 {"created", "updated", "removed"}；云端调用失败时向上抛出。
    """
    counts = {"created": 0, "updated": 0, "removed": 0}
    if not gateways or client is None:
        return counts
    gateway = gateways[0]
    cloud_items = client.list_consumer_groups()

    existing = {
        row.external_consumer_group_id: row
        for row in db.exec(
            select(ApiKeyConsumerGroup).where(
                ApiKeyConsumerGroup.tenant_id == tenant_id
            )
        ).all()
        if row.external_consumer_group_id
    }

    now = utc_now()
    cloud_ids: set[str] = set()
    for item in cloud_items:
        csg_id = item.get("consumerGroupId") or ""
        if not csg_id:
            continue
        cloud_ids.add(csg_id)
        if csg_id in existing:
            row = existing[csg_id]
            row.name = item.get("name") or row.name
            row.description = item.get("description")
            row.consumer_count = item.get("consumerCount")
            row.updated_at = now
            counts["updated"] += 1
        else:
            db.add(
                ApiKeyConsumerGroup(
                    tenant_id=tenant_id,
                    name=item.get("name") or "",
                    description=item.get("description"),
                    gateway_id=gateway.gateway_id,
                    gateway_name=gateway.name,
                    external_consumer_group_id=csg_id,
                    consumer_type=item.get("gatewayType") or "AI",
                    consumer_count=item.get("consumerCount"),
                    created_at=now,
                    updated_at=now,
                )
            )
            counts["created"] += 1

    # 完整镜像模式：云端已删除的消费组本地同步移除
    if mirror:
        for csg_id, row in existing.items():
            if csg_id not in cloud_ids:
                db.delete(row)
                counts["removed"] += 1
    db.commit()
    return counts


def _sync_consumers_to_local(
    db: Session,
    tenant_id: str,
    client,
    gateways: list,
    mirror: bool = False,
) -> dict[str, int]:
    """从阿里云同步消费者到本地 DB（幂等）。

    mirror=False（隐式同步）：仅新增/更新；归属变更只在云端有值时跟随。
    mirror=True（手动同步按钮）：完整镜像语义——归属以云端为准（移出组则
    清空本地归属），云端已删除的消费者同步移除。
    返回变更计数；云端调用失败时向上抛出。
    """
    counts = {"created": 0, "updated": 0, "removed": 0}
    if not gateways or client is None:
        return counts
    gateway = gateways[0]
    cloud_items = client.list_consumers()

    existing = {
        row.external_consumer_id: row
        for row in db.exec(
            select(ApiKeyConsumer).where(
                ApiKeyConsumer.tenant_id == tenant_id
            )
        ).all()
        if row.external_consumer_id
    }

    now = utc_now()
    cloud_ids: set[str] = set()
    for item in cloud_items:
        consumer_id = item.get("consumerId") or ""
        if not consumer_id:
            continue
        cloud_ids.add(consumer_id)

        # 消费组归属取自云端消费者详情；详情获取失败时保留本地归属
        csg_name = ""
        csg_id = ""
        try:
            detail = client.get_consumer(consumer_id)
            consumer_groups_info = detail.get("consumerGroups") or []
            if consumer_groups_info:
                first_csg = (
                    consumer_groups_info[0]
                    if isinstance(consumer_groups_info, list)
                    else consumer_groups_info
                )
                if isinstance(first_csg, dict):
                    csg_name = first_csg.get("name") or ""
                    csg_id = first_csg.get("consumerGroupId") or ""
        except (AliyunApigError, RuntimeError):
            csg_id = None  # 详情失败：无法判定，保持本地值
            csg_name = None

        enable = item.get("enable", True)
        if consumer_id in existing:
            row = existing[consumer_id]
            row.name = item.get("name") or row.name
            row.description = item.get("description")
            row.enable = enable
            row.deploy_status = item.get("deployStatus")
            if csg_id is not None:
                if mirror:
                    # 完整镜像：云端为准，移出组则清空本地归属
                    row.external_consumer_group_id = csg_id or None
                    row.consumer_group_name = csg_name or None
                else:
                    # 隐式同步：云端无归属时保留本地值
                    row.external_consumer_group_id = csg_id or row.external_consumer_group_id
                    row.consumer_group_name = csg_name or row.consumer_group_name
            row.status = "enabled" if enable else "disabled"
            row.updated_at = now
            counts["updated"] += 1
        else:
            db.add(
                ApiKeyConsumer(
                    tenant_id=tenant_id,
                    name=item.get("name") or "",
                    description=item.get("description"),
                    gateway_id=gateway.gateway_id,
                    gateway_name=gateway.name,
                    external_consumer_id=consumer_id,
                    external_consumer_group_id=csg_id or None,
                    consumer_group_name=csg_name or None,
                    consumer_type=item.get("gatewayType") or "AI",
                    deploy_status=item.get("deployStatus"),
                    enable=enable,
                    status="enabled" if enable else "disabled",
                    created_at=now,
                    updated_at=now,
                )
            )
            counts["created"] += 1

    # 完整镜像模式：云端已删除的消费者本地同步移除
    if mirror:
        for consumer_id, row in existing.items():
            if consumer_id not in cloud_ids:
                db.delete(row)
                counts["removed"] += 1
    db.commit()
    return counts


def _sync_quota_rules_to_local(
    db: Session,
    tenant_id: str,
    client,
    gateways: list,
    mirror: bool = False,
) -> dict[str, int]:
    """从阿里云同步配额规则到本地 DB（幂等）。

    mirror=False（隐式同步）：仅新增/更新。mirror=True（手动同步按钮）：
    完整镜像语义，云端已删除的规则同步移除。
    返回变更计数；云端调用失败时向上抛出。
    """
    counts = {"created": 0, "updated": 0, "removed": 0}
    if not gateways or client is None:
        return counts
    gateway = gateways[0]

    cloud_items = client.list_quota_rules(gateway.gateway_id)

    existing = {
        row.external_rule_id: row
        for row in db.exec(
            select(ApiKeyQuotaRule).where(
                ApiKeyQuotaRule.tenant_id == tenant_id
            )
        ).all()
        if row.external_rule_id
    }

    now = utc_now()
    cloud_ids: set[str] = set()
    for item in cloud_items:
        rule_id = item.get("ruleId") or ""
        if not rule_id:
            continue
        cloud_ids.add(rule_id)

        if rule_id in existing:
            row = existing[rule_id]
            row.name = item.get("ruleName") or row.name
            row.quota_dimension = item.get("quotaDimension") or row.quota_dimension
            row.quota_limit = int(item.get("quotaLimit") or row.quota_limit)
            row.period_type = item.get("periodType") or row.period_type
            row.status = item.get("ruleStatus") or row.status
            row.updated_at = now
            counts["updated"] += 1
        else:
            db.add(
                ApiKeyQuotaRule(
                    tenant_id=tenant_id,
                    name=item.get("ruleName") or "",
                    gateway_id=gateway.gateway_id,
                    gateway_name=gateway.name,
                    quota_dimension=item.get("quotaDimension") or "credit",
                    quota_limit=int(item.get("quotaLimit") or 0),
                    period_type=item.get("periodType") or "day",
                    external_rule_id=rule_id,
                    status=item.get("ruleStatus") or "enabled",
                    created_at=now,
                    updated_at=now,
                )
            )
            counts["created"] += 1

    # 回填主体粒度（consumer / consumer_group）：ListGatewayQuotaRules 不返回
    # subjectType，需按规则单独调 GetGatewayQuotaRule；只查新增行与存量空值行，
    # 查询失败按消费者粒度兜底（兼容旧行为），成功后落库避免下次重复调用。
    rows_needing_subject = [
        row
        for row in list(existing.values())
        + [r for r in db.new if isinstance(r, ApiKeyQuotaRule) and r.external_rule_id]
        if row.external_rule_id and not row.subject_type
    ]
    if rows_needing_subject:
        for row in rows_needing_subject:
            try:
                detail = client.get_quota_rule(row.external_rule_id, gateway_id=gateway.gateway_id)
            except (AliyunApigError, RuntimeError, ValueError):
                row.subject_type = row.subject_type or "consumer"
                continue
            row.subject_type = detail.get("subjectType") or "consumer"
            row.updated_at = now

    # 完整镜像模式：云端已删除的配额规则本地同步移除
    if mirror:
        for rule_id, row in existing.items():
            if rule_id not in cloud_ids:
                db.delete(row)
                counts["removed"] += 1
    db.commit()
    return counts


def _cg_from_local(row: ApiKeyConsumerGroup) -> ApiKeyConsumerGroupRead:
    """本地消费组记录 -> 读取模型。"""
    return ApiKeyConsumerGroupRead(
        id=row.external_consumer_group_id or row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        owner=row.owner,
        gateway_id=row.gateway_id,
        gateway_name=row.gateway_name,
        external_consumer_group_id=row.external_consumer_group_id,
        consumer_type=row.consumer_type,
        consumer_count=row.consumer_count,
        status=row.status,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


def _co_from_local(row: ApiKeyConsumer) -> ApiKeyConsumerRead:
    """本地消费者记录 -> 读取模型。"""
    return ApiKeyConsumerRead(
        id=row.external_consumer_id or row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        gateway_id=row.gateway_id,
        gateway_name=row.gateway_name,
        external_consumer_id=row.external_consumer_id,
        external_consumer_group_id=row.external_consumer_group_id,
        consumer_group_name=row.consumer_group_name,
        consumer_type=row.consumer_type,
        deploy_status=row.deploy_status,
        enable=row.enable,
        status=row.status,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


def _qr_from_local(row: ApiKeyQuotaRule) -> ApiKeyQuotaRuleRead:
    """本地配额规则记录 -> 读取模型。"""
    return ApiKeyQuotaRuleRead(
        id=row.external_rule_id or row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        gateway_id=row.gateway_id,
        gateway_name=row.gateway_name,
        quota_dimension=row.quota_dimension,
        quota_limit=row.quota_limit,
        period_type=row.period_type,
        external_rule_id=row.external_rule_id,
        subject_type=row.subject_type or "consumer",
        status=row.status,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )

# ============ 路由端点 ============

MAX_APPLICATIONS_PER_USER = 2
ACTIVE_STATUSES = ("pending", "approved")
# 审批列表排序:pending 优先,其余按时间倒序
_STATUS_ORDER = {"pending": 0, "approved": 1, "rejected": 2, "revoked": 3}
_VALID_PERIODS = {"day", "week", "month"}
# 阿里云 AI 网关配额维度：token / credit（现网规则 AILAB 使用 credit）
_VALID_DIMENSIONS = {"token", "credit"}


class ApiKeyApplicationCreate(BaseModel):
    tenant_id: str
    purpose: str | None = None


class ApiKeyApplicationReview(BaseModel):
    tenant_id: str
    reviewer_note: str | None = None


class ApiKeyApplicationApprove(BaseModel):
    tenant_id: str
    consumer_name: str  # 审批时新建消费者的名称（如 ailab_zhangwei）
    consumer_group_id: str  # 审批时选定的消费组（云端 csg-）
    quota_rule_id: str  # 审批时选定的配额规则
    api_url: str | None = None  # 下发给申请人的网关地址（可选，留空用默认值）


class GatewayInfo(BaseModel):
    name: str
    gateway_id: str
    gateway_url: str


class ApiKeyConsumerGroupCreate(BaseModel):
    """消费者组创建（已废弃 — 消费者组不可增删改，仅同步自阿里云）。"""
    tenant_id: str
    name: str


class ApiKeyConsumerGroupUpdate(BaseModel):
    """消费者组更新（已废弃 — 消费者组不可增删改）。"""
    tenant_id: str
    name: str | None = None


class ApiKeyConsumerGroupRead(BaseModel):
    """消费者组读取模型。"""
    id: str
    tenant_id: str
    name: str
    description: str | None = None
    owner: str | None = None
    gateway_id: str | None = None
    gateway_name: str | None = None
    external_consumer_group_id: str | None = None  # 阿里云消费组 ID
    external_consumer_id: str | None = None  # [废弃]
    consumer_type: str = "AI"
    consumer_count: int | None = None  # 组内消费者数量
    status: str = "enabled"
    created_by_user_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ApiKeyConsumerRead(BaseModel):
    """消费者读取模型。"""
    id: str
    tenant_id: str
    name: str
    description: str | None = None
    gateway_id: str | None = None
    gateway_name: str | None = None
    external_consumer_id: str | None = None  # 阿里云消费者 ID (cs-)
    external_consumer_group_id: str | None = None  # 所属消费组 (csg-)
    consumer_group_name: str | None = None
    consumer_type: str = "AI"
    deploy_status: str | None = None
    enable: bool = True
    status: str = "enabled"
    created_by_user_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ApiKeyConsumerToggle(BaseModel):
    """消费者启用/停用。"""
    tenant_id: str
    enable: bool


class ApiKeyConsumerQuotaUpdate(BaseModel):
    """消费者修改配额规则。"""
    tenant_id: str
    quota_rule_id: str


class ApiKeyQuotaRuleCreate(BaseModel):
    tenant_id: str
    name: str
    gateway_name: str  # 需在网关列表中存在
    quota_dimension: str = "credit"  # token / credit
    quota_limit: int  # 配额上限
    period_type: str = "month"  # day / week / month
    subject_type: str = "consumer"  # consumer（按消费者逐个绑定）/ consumer_group（按消费组整组绑定）
    consumer_ids: list[str] = []  # subject_type=consumer 时可选：创建时直接绑定的消费者（cs-）
    consumer_group_ids: list[str] = []  # subject_type=consumer_group 时生效
    description: str | None = None
    owner: str | None = None  # 业务归属（非阿里云字段）


class ApiKeyQuotaRuleUpdate(BaseModel):
    tenant_id: str
    name: str | None = None
    quota_limit: int | None = None
    period_type: str | None = None
    # 绑定消费组增删（组粒度主体）；消费者主体数量可能很多，改消费者请到阿里云控制台
    add_group_ids: list[str] = []
    remove_group_ids: list[str] = []


class ApiKeyQuotaRuleRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    gateway_id: str | None
    gateway_name: str | None
    quota_dimension: str
    quota_limit: int
    period_type: str
    external_rule_id: str | None
    subject_type: str | None = None  # consumer / consumer_group（组粒度整组共享限额）
    status: str
    created_by_user_id: str | None
    created_at: str
    updated_at: str


class ApiKeyApplicationRead(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    username: str | None
    purpose: str | None
    status: str
    api_key_masked: str | None
    api_key: str | None = None  # 仅申请人本人查看已批准申请时返回明文
    api_url: str | None = None
    gateway_name: str | None = None
    gateway_id: str | None = None
    quota_limit: int | None = None
    quota_period: str | None = None
    quota_rule_id: str | None = None
    quota_rule_name: str | None = None
    consumer_id: str | None = None
    consumer_name: str | None = None
    consumer_status: str | None = None  # enabled / disabled / deleted(消费者已被删除) / None(未知)，异常时前端提示
    consumer_group_id: str | None = None
    consumer_group_name: str | None = None
    used_amount: int | None = None
    usage_month: str | None = None
    reviewer_note: str | None
    reviewed_at: str | None
    created_at: str
    updated_at: str


class ApiKeyApprovalStats(BaseModel):
    pending: int
    allocated: int
    history: int


class ApiKeyQuotaUpdate(BaseModel):
    tenant_id: str
    quota_limit: int


class ApiKeyApplicationUsageItem(BaseModel):
    id: str
    user_id: str
    username: str | None
    user_no: str | None = None
    department: str | None = None
    consumer_id: str | None = None
    consumer_name: str | None = None
    gateway_name: str | None = None
    gateway_id: str | None = None
    quota_limit: int | None = None
    quota_period: str | None = None
    quota_rule_id: str | None = None
    used_amount: int
    usage_rate: float  # 0.0 ~ 1.0
    suggestion: str  # expand / normal / watch / unknown


class ApiKeyUsageSummary(BaseModel):
    allocated_users: int
    total_quota: int
    total_used: int
    avg_usage_rate: float
    high_watermark_users: int
    low_watermark_users: int


class ApiKeyApplicationUsageRead(BaseModel):
    month: str
    summary: ApiKeyUsageSummary
    items: list[ApiKeyApplicationUsageItem]


def _read(
    row: ApiKeyApplication,
    include_secret: bool = False,
    consumer_status: str | None = None,
) -> ApiKeyApplicationRead:
    # 历史数据可能用旧 APP_SECRET 加密：容错解密，失败按「密钥失效」处理（masked 为空）
    plain_key = try_decrypt_secret(row.api_key_encrypted)
    return ApiKeyApplicationRead(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        username=row.username,
        purpose=row.purpose,
        status=row.status,
        api_key_masked=mask_secret(plain_key) if plain_key else None,
        api_key=plain_key if (include_secret and plain_key) else None,
        api_url=row.api_url,
        gateway_name=row.gateway_name,
        gateway_id=row.gateway_id,
        quota_limit=row.quota_limit,
        quota_period=row.quota_period,
        quota_rule_id=row.quota_rule_id,
        quota_rule_name=row.quota_rule_name,
        consumer_id=row.consumer_id,
        consumer_name=row.consumer_name,
        consumer_status=consumer_status,
        consumer_group_id=row.consumer_group_id,
        consumer_group_name=row.consumer_group_name,
        used_amount=row.used_amount,
        usage_month=row.usage_month,
        reviewer_note=row.reviewer_note,
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _generate_api_key() -> str:
    """系统生成强随机 API Key 明文（审批流程以 Custom 模式写入阿里云消费者）。"""
    return "sk-" + secrets.token_urlsafe(32)


@router.get("/gateways", response_model=list[GatewayInfo])
def list_gateways() -> list[GatewayInfo]:
    """返回后端配置的网关列表(名称/gatewayId/调用地址),供审批时选择。"""
    try:
        configs = get_gateway_configs()
    except AliyunApigError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return [
        GatewayInfo(name=cfg.name, gateway_id=cfg.gateway_id, gateway_url=cfg.gateway_url)
        for cfg in configs
    ]


@router.get("/stats", response_model=ApiKeyApprovalStats)
def approval_stats(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApprovalStats:
    """密钥审核页顶部统计:待审核 / 已分配 / 审核历史。"""
    ensure_tenant_admin(tenant_id, current_user)
    rows = db.exec(
        select(ApiKeyApplication).where(ApiKeyApplication.tenant_id == tenant_id)
    ).all()
    pending = sum(1 for r in rows if r.status == "pending")
    allocated = sum(1 for r in rows if r.status == "approved")
    history = sum(1 for r in rows if r.status != "pending")
    return ApiKeyApprovalStats(pending=pending, allocated=allocated, history=history)


@router.get("/consumer-group-owners", response_model=dict[str, list[str]])
def list_consumer_group_owners(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
) -> dict[str, list[str]]:
    """返回消费组「归属」业务字段的可选值（来自 CONSUMER_GROUP_OWNERS 配置）。"""
    ensure_tenant_admin(tenant_id, current_user)
    return {"owners": get_settings().consumer_group_owner_list}


@router.get("/consumer-groups", response_model=list[ApiKeyConsumerGroupRead])
def list_consumer_groups(
    tenant_id: str = Query(...),
    refresh: bool = Query(False, description="跳过 Redis 同步节流，强制回源阿里云"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ApiKeyConsumerGroupRead]:
    """列出消费组:从阿里云同步到本地,再从本地读取。消费组不可增删改。"""
    ensure_tenant_admin(tenant_id, current_user)
    gateways = get_gateway_configs()
    if gateways:
        try:
            client = get_apig_client()
            # 隐式同步节流：TTL 内已有同步则跳过（只 upsert 不删，跳过不改变语义）；
            # 显式「同步阿里云」/refresh=1 会清除节流标记强制回源
            if refresh or not acquire_sync_throttle(f"{tenant_id}:sync:consumer-groups"):
                _sync_consumer_groups_to_local(db, tenant_id, client, gateways)
        except (AliyunApigError, RuntimeError):
            pass

    rows = db.exec(
        select(ApiKeyConsumerGroup)
        .where(ApiKeyConsumerGroup.tenant_id == tenant_id)
        .order_by(ApiKeyConsumerGroup.name)
    ).all()
    return [_cg_from_local(row) for row in rows]


class ApiKeyConsumerGroupOwnerUpdate(BaseModel):
    """修改消费组「归属」。归属是纯业务字段（本地记录），不联动阿里云。"""

    tenant_id: str
    owner: str | None = None  # 传空表示清空归属


@router.put("/consumer-groups/{group_id}/owner", response_model=ApiKeyConsumerGroupRead)
def update_consumer_group_owner(
    group_id: str,
    request: ApiKeyConsumerGroupOwnerUpdate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyConsumerGroupRead:
    """修改消费组「归属」（业务数据）：仅更新本地记录，不调用阿里云 API。

    group_id 优先按云端消费组 ID（csg-）匹配，兼容纯本地记录的主键。
    阿里云同步只覆盖云端字段（名称/描述/成员数），不会覆盖归属。
    """
    ensure_tenant_admin(request.tenant_id, current_user)
    row = db.exec(
        select(ApiKeyConsumerGroup).where(
            ApiKeyConsumerGroup.tenant_id == request.tenant_id,
            ApiKeyConsumerGroup.external_consumer_group_id == group_id,
        )
    ).first()
    if row is None:
        row = db.get(ApiKeyConsumerGroup, group_id)
        if row is None or row.tenant_id != request.tenant_id:
            raise HTTPException(status_code=404, detail="消费组不存在")
    row.owner = (request.owner or "").strip() or None
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _cg_from_local(row)


class ApiKeyAliyunSyncRequest(BaseModel):
    tenant_id: str


class ApiKeyAliyunSyncResult(BaseModel):
    synced_at: str
    groups: dict[str, int]
    consumers: dict[str, int]
    quota_rules: dict[str, int]


@router.post("/consumer-groups/sync", response_model=ApiKeyAliyunSyncResult)
def sync_consumer_groups_from_aliyun(
    request: ApiKeyAliyunSyncRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyAliyunSyncResult:
    """手动触发：从阿里云全量同步消费组/消费者/配额规则到本地（云端为准）。

    用于处理在阿里云控制台手动操作（建组、删除、调整成员/规则）导致的本地数据漂移。
    """
    ensure_tenant_admin(request.tenant_id, current_user)
    gateways = get_gateway_configs()
    if not gateways:
        raise HTTPException(status_code=400, detail="后端未配置网关（ALIYUN_APIG_GATEWAYS）")
    client = _require_apig_client("同步阿里云数据")
    try:
        groups = _sync_consumer_groups_to_local(db, request.tenant_id, client, gateways, mirror=True)
        consumers = _sync_consumers_to_local(db, request.tenant_id, client, gateways, mirror=True)
        quota_rules = _sync_quota_rules_to_local(db, request.tenant_id, client, gateways, mirror=True)
    except (AliyunApigError, RuntimeError) as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc
    # 显式同步（mirror，云端为准）成功：清掉该租户的节流/缓存标记，
    # 让后续隐式 GET 立即回源校准，同时清除节流窗口内的滞后
    invalidate_tenant(request.tenant_id)
    return ApiKeyAliyunSyncResult(
        synced_at=utc_now().isoformat(),
        groups=groups,
        consumers=consumers,
        quota_rules=quota_rules,
    )

# ============ 消费者端点 ============


@router.get("/consumers", response_model=list[ApiKeyConsumerRead])
def list_consumers_endpoint(
    tenant_id: str = Query(...),
    refresh: bool = Query(False, description="跳过 Redis 同步节流，强制回源阿里云"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ApiKeyConsumerRead]:
    """列出消费者:从阿里云同步到本地,再从本地读取。"""
    ensure_tenant_admin(tenant_id, current_user)
    gateways = get_gateway_configs()
    if gateways:
        try:
            client = get_apig_client()
            if refresh or not acquire_sync_throttle(f"{tenant_id}:sync:consumers"):
                _sync_consumers_to_local(db, tenant_id, client, gateways)
        except (AliyunApigError, RuntimeError):
            pass

    rows = db.exec(
        select(ApiKeyConsumer)
        .where(ApiKeyConsumer.tenant_id == tenant_id)
        .order_by(ApiKeyConsumer.name)
    ).all()
    return [_co_from_local(row) for row in rows]


@router.post("/consumers/{consumer_id}/toggle", response_model=ApiKeyConsumerRead)
def toggle_consumer(
    consumer_id: str,
    request: ApiKeyConsumerToggle,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyConsumerRead:
    """启用或停用消费者。consumer_id 为云端 consumerId（cs-）。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    client = _require_apig_client("启用/停用消费者")
    gateways = get_gateway_configs()
    gateway = gateways[0] if gateways else None
    try:
        client.update_consumer(consumer_id=consumer_id, enable=request.enable)
    except (AliyunApigError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    row = db.exec(
        select(ApiKeyConsumer).where(
            ApiKeyConsumer.tenant_id == request.tenant_id,
            ApiKeyConsumer.external_consumer_id == consumer_id,
        )
    ).first()
    if row:
        row.enable = request.enable
        row.status = "enabled" if request.enable else "disabled"
        row.updated_at = utc_now()
        db.add(row)
        db.commit()
        db.refresh(row)
    return _co_from_local(row) if row else ApiKeyConsumerRead(
        id=consumer_id, tenant_id=request.tenant_id, name=consumer_id,
        enable=request.enable, status="enabled" if request.enable else "disabled",
    )


@router.post("/consumers/{consumer_id}/quota", response_model=ApiKeyConsumerRead)
def consumer_change_quota(
    consumer_id: str,
    request: ApiKeyConsumerQuotaUpdate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyConsumerRead:
    """修改消费者绑定的配额规则。consumer_id 为云端 consumerId（cs-）。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    client = _require_apig_client("修改消费者配额")
    gateways = get_gateway_configs()
    if not gateways:
        raise HTTPException(status_code=400, detail="后端未配置网关（ALIYUN_APIG_GATEWAYS）")
    gateway = gateways[0]
    # 目标规则若是消费组粒度（subjectType=consumer_group），消费者换绑应转成
    # 把消费者所在消费组 addIds 到该规则（或提示走组粒度）；此处先查 subjects
    try:
        rule_is_group_scoped = any(
            s.get("subjectType") == "consumer_group"
            for s in client.list_quota_rule_subjects(
                request.quota_rule_id, gateway_id=gateway.gateway_id
            )
        )
    except (AliyunApigError, RuntimeError):
        rule_is_group_scoped = False
    if rule_is_group_scoped:
        # 组粒度规则：把消费者归属的消费组绑到规则（找不到归属组则报错）
        consumer_row = db.exec(
            select(ApiKeyConsumer).where(
                ApiKeyConsumer.tenant_id == request.tenant_id,
                ApiKeyConsumer.external_consumer_id == consumer_id,
            )
        ).first()
        group_id = consumer_row.external_consumer_group_id if consumer_row else None
        if not group_id:
            raise HTTPException(
                status_code=409,
                detail="该配额规则为消费组粒度，且消费者未归属任何消费组，无法绑定；请先将其加入消费组",
            )
        try:
            client.attach_consumer_group_to_rule(
                gateway_id=gateway.gateway_id,
                rule_id=request.quota_rule_id,
                consumer_group_id=group_id,
            )
        except (AliyunApigError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc
    else:
        try:
            client.attach_consumer_to_rule(
                gateway_id=gateway.gateway_id,
                rule_id=request.quota_rule_id,
                consumer_id=consumer_id,
            )
        except (AliyunApigError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    row = db.exec(
        select(ApiKeyConsumer).where(
            ApiKeyConsumer.tenant_id == request.tenant_id,
            ApiKeyConsumer.external_consumer_id == consumer_id,
        )
    ).first()
    if row:
        row.updated_at = utc_now()
        db.add(row)
        db.commit()
        db.refresh(row)
    return _co_from_local(row) if row else ApiKeyConsumerRead(
        id=consumer_id, tenant_id=request.tenant_id, name=consumer_id,
    )


@router.get("/quota-rules", response_model=list[ApiKeyQuotaRuleRead])
def list_quota_rules(
    tenant_id: str = Query(...),
    refresh: bool = Query(False, description="跳过 Redis 同步节流，强制回源阿里云"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ApiKeyQuotaRuleRead]:
    """列出配额规则:先从阿里云同步到本地,再从本地读取。"""
    ensure_tenant_admin(tenant_id, current_user)
    gateways = get_gateway_configs()
    if gateways:
        try:
            client = get_apig_client()
            if refresh or not acquire_sync_throttle(f"{tenant_id}:sync:quota-rules"):
                _sync_quota_rules_to_local(db, tenant_id, client, gateways)
        except (AliyunApigError, RuntimeError):
            pass

    rows = db.exec(
        select(ApiKeyQuotaRule)
        .where(ApiKeyQuotaRule.tenant_id == tenant_id)
        .order_by(ApiKeyQuotaRule.name, ApiKeyQuotaRule.created_at.desc())
    ).all()
    return [_qr_from_local(row) for row in rows]


@router.post("/quota-rules", response_model=ApiKeyQuotaRuleRead)
def create_quota_rule(
    request: ApiKeyQuotaRuleCreate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyQuotaRuleRead:
    """创建配额规则:在指定网关下创建一个阿里云 QuotaRule。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    if request.period_type not in _VALID_PERIODS:
        raise HTTPException(status_code=400, detail="period_type 必须是 day / week / month 之一")
    if not isinstance(request.quota_limit, int) or request.quota_limit <= 0:
        raise HTTPException(status_code=400, detail="quota_limit 必须为正整数")
    gateway = get_gateway_config(request.gateway_name)
    if not gateway:
        raise HTTPException(
            status_code=400,
            detail=f"未知网关名称「{request.gateway_name}」，请检查后端 ALIYUN_APIG_GATEWAYS 配置",
        )
    if request.quota_dimension not in _VALID_DIMENSIONS:
        raise HTTPException(status_code=400, detail="quota_dimension 必须是 token / credit 之一")
    if request.subject_type not in ("consumer", "consumer_group"):
        raise HTTPException(status_code=400, detail="subject_type 必须是 consumer / consumer_group 之一")
    if request.subject_type == "consumer_group" and not request.consumer_group_ids:
        raise HTTPException(status_code=400, detail="subject_type=consumer_group 时必须提供 consumer_group_ids")
    client = _require_apig_client("创建配额规则")
    try:
        # 两种主体粒度（AI 网关 2.1.21+ 支持消费组粒度，2.1.23 可用）：
        # - consumer：按消费者逐个绑定（consumerIds，可选——不传则创建空规则，
        #   后续审批/阿里云控制台再绑）
        # - consumer_group：按消费组整组绑定（consumerGroupIds，与 consumerIds 互斥）
        rule_id = client.add_consumer_quota_rule(
            gateway_id=gateway.gateway_id,
            consumer_ids=(
                request.consumer_ids if request.subject_type == "consumer" else []
            ),
            quota_limit=request.quota_limit,
            period_type=request.period_type,
            rule_name=request.name,
            timezone="UTC+8",
            quota_dimension=request.quota_dimension,
            consumer_group_ids=(
                request.consumer_group_ids if request.subject_type == "consumer_group" else None
            ),
        )
    except (AliyunApigError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    # 本地仅留补充记录（创建人等），名称/额度/周期真值以云端为准
    row = ApiKeyQuotaRule(
        tenant_id=request.tenant_id,
        name=request.name,
        gateway_id=gateway.gateway_id,
        gateway_name=gateway.name,
        quota_dimension=request.quota_dimension,
        quota_limit=request.quota_limit,
        period_type=request.period_type,
        external_rule_id=rule_id,
        subject_type=request.subject_type,
        created_by_user_id=current_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    try:
        detail = client.get_quota_rule(rule_id, gateway_id=gateway.gateway_id)
    except (AliyunApigError, RuntimeError):
        detail = {
            "ruleId": rule_id,
            "ruleName": request.name,
            "quotaDimension": request.quota_dimension,
            "quotaLimit": request.quota_limit,
            "periodType": request.period_type,
        }
    return _qr_read_live(detail, tenant_id=request.tenant_id, gateway=gateway, local=row)


@router.put("/quota-rules/{rule_id}", response_model=ApiKeyQuotaRuleRead)
def update_quota_rule_endpoint(
    rule_id: str,
    request: ApiKeyQuotaRuleUpdate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyQuotaRuleRead:
    """编辑配额规则(rule_id 即云端 ruleId):更新规则名称/额度/周期(并同步阿里云)。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    if request.period_type and request.period_type not in _VALID_PERIODS:
        raise HTTPException(status_code=400, detail="period_type 必须是 day / week / month 之一")
    if request.quota_limit is not None and (not isinstance(request.quota_limit, int) or request.quota_limit <= 0):
        raise HTTPException(status_code=400, detail="quota_limit 必须为正整数")

    client = _require_apig_client("编辑配额规则")
    gateways = get_gateway_configs()
    if not gateways:
        raise HTTPException(status_code=400, detail="后端未配置网关（ALIYUN_APIG_GATEWAYS）")
    gateway = gateways[0]
    row = db.exec(
        select(ApiKeyQuotaRule).where(
            ApiKeyQuotaRule.tenant_id == request.tenant_id,
            ApiKeyQuotaRule.external_rule_id == rule_id,
        )
    ).first()
    # 先取云端当前值，便于只更新传了的字段
    try:
        current = client.get_quota_rule(rule_id, gateway_id=gateway.gateway_id)
    except (AliyunApigError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    try:
        if request.quota_limit is not None:
            client.update_consumer_quota_rule(
                gateway_id=gateway.gateway_id,
                rule_id=rule_id,
                quota_limit=request.quota_limit,
            )
        if request.name is not None or request.period_type is not None:
            client.update_quota_rule_meta(
                gateway_id=gateway.gateway_id,
                rule_id=rule_id,
                rule_name=request.name if request.name is not None else current.get("ruleName"),
                period_type=(
                    request.period_type
                    if request.period_type is not None
                    else current.get("periodType")
                ),
            )
        # 绑定消费组增删（组粒度主体；消费者主体请到阿里云控制台调整）
        for gid in request.add_group_ids:
            client.attach_consumer_group_to_rule(
                gateway_id=gateway.gateway_id, rule_id=rule_id, consumer_group_id=gid
            )
        for gid in request.remove_group_ids:
            client.detach_consumer_group_from_rule(
                gateway_id=gateway.gateway_id, rule_id=rule_id, consumer_group_id=gid
            )
    except (AliyunApigError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    if row is None:
        row = ApiKeyQuotaRule(
            tenant_id=request.tenant_id,
            name=request.name or current.get("ruleName") or "",
            gateway_id=gateway.gateway_id,
            gateway_name=gateway.name,
        quota_dimension=current.get("quotaDimension") or "credit",
        quota_limit=request.quota_limit or int(current.get("quotaLimit") or 0),
        period_type=request.period_type or current.get("periodType") or "day",
        external_rule_id=rule_id,
        subject_type=current.get("subjectType") or "consumer",
        created_by_user_id=current_user.id,
    )
    if request.name is not None:
        row.name = request.name
    if request.quota_limit is not None:
        row.quota_limit = request.quota_limit
    if request.period_type is not None:
        row.period_type = request.period_type
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)

    try:
        detail = client.get_quota_rule(rule_id, gateway_id=gateway.gateway_id)
    except (AliyunApigError, RuntimeError):
        detail = current
    return _qr_read_live(detail, tenant_id=request.tenant_id, gateway=gateway, local=row)


@router.get("/quota-rules/{rule_id}/subjects")
def list_quota_rule_subjects_endpoint(
    rule_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """查询配额规则当前绑定的主体（编辑弹窗回显）。

    返回 {"groups": [csg-...], "consumer_count": n}：仅回显消费组主体
    （消费者数量可能很多，编辑端不展示，去阿里云控制台调整）。
    """
    ensure_tenant_admin(tenant_id, current_user)
    client = _require_apig_client("查询配额规则主体")
    gateways = get_gateway_configs()
    if not gateways:
        raise HTTPException(status_code=400, detail="后端未配置网关（ALIYUN_APIG_GATEWAYS）")
    gateway = gateways[0]
    try:
        subjects = client.list_quota_rule_subjects(rule_id, gateway_id=gateway.gateway_id)
    except (AliyunApigError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc
    groups: list[str] = []
    consumer_count = 0
    for subj in subjects if isinstance(subjects, list) else []:
        if subj.get("subjectType") == "consumer_group":
            sid = subj.get("id") or ""
            if sid and sid not in groups:
                groups.append(sid)
        else:
            consumer_count += 1
    return {"groups": groups, "consumer_count": consumer_count}

# ============ 审批/申请/吊销/用量 端点 ============


def _ms_to_naive_iso(ms: object) -> str:
    """阿里云返回的毫秒时间戳 -> 与本地行一致的 naive UTC ISO 字符串。"""
    if not ms:
        return utc_now().isoformat()
    try:
        return (
            datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
            .replace(tzinfo=None)
            .isoformat()
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return utc_now().isoformat()


def _qr_read_live(
    item: dict,
    *,
    tenant_id: str,
    gateway,
    local: ApiKeyQuotaRule | None = None,
) -> ApiKeyQuotaRuleRead:
    """阿里云配额规则 -> 配额规则读取模型（id 直接用云端 ruleId）。"""
    rule_id = item.get("ruleId") or ""
    return ApiKeyQuotaRuleRead(
        id=rule_id,
        tenant_id=tenant_id,
        name=item.get("ruleName") or "",
        gateway_id=gateway.gateway_id if gateway else None,
        gateway_name=gateway.name if gateway else None,
        quota_dimension=item.get("quotaDimension") or "credit",
        quota_limit=int(item.get("quotaLimit") or 0),
        period_type=item.get("periodType") or "day",
        external_rule_id=rule_id,
        subject_type=item.get("subjectType") or "consumer",
        status=item.get("ruleStatus") or "enabled",
        created_by_user_id=(local.created_by_user_id if local else None),
        created_at=_ms_to_naive_iso(item.get("createTimestamp")),
        updated_at=_ms_to_naive_iso(item.get("updateTimestamp")),
    )


def _require_apig_client(action: str):
    """获取阿里云客户端，未配置凭据时返回 503。"""
    client = get_apig_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=f"后端未配置阿里云 AK/SK（ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET），无法{action}",
        )
    return client


@router.post("", response_model=ApiKeyApplicationRead)
def create_application(
    request: ApiKeyApplicationCreate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    ensure_tenant(db, request.tenant_id)
    active_rows = db.exec(
        select(ApiKeyApplication).where(
            ApiKeyApplication.tenant_id == request.tenant_id,
            ApiKeyApplication.user_id == current_user.id,
            ApiKeyApplication.status.in_(ACTIVE_STATUSES),
        )
    ).all()
    # 上限口径 = 在用/申请中；approved 但关联消费者已被删除的记录不占名额
    active_consumer_ids = {
        c.external_consumer_id
        for c in db.exec(
            select(ApiKeyConsumer).where(ApiKeyConsumer.tenant_id == request.tenant_id)
        ).all()
        if c.external_consumer_id
    }
    active = [
        r
        for r in active_rows
        if r.status == "pending" or (r.consumer_id and r.consumer_id in active_consumer_ids)
    ]
    if len(active) >= MAX_APPLICATIONS_PER_USER:
        raise HTTPException(
            status_code=409,
            detail=f"每位用户最多申请 {MAX_APPLICATIONS_PER_USER} 个 API Key（当前已有 {len(active)} 个待审/已批准）",
        )
    row = ApiKeyApplication(
        tenant_id=request.tenant_id,
        user_id=current_user.id,
        username=current_user.username,
        purpose=(request.purpose or "").strip() or None,
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row)


@router.get("/mine", response_model=list[ApiKeyApplicationRead])
def list_my_applications(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ApiKeyApplicationRead]:
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(ApiKeyApplication)
        .where(
            ApiKeyApplication.tenant_id == tenant_id,
            ApiKeyApplication.user_id == current_user.id,
        )
        .order_by(ApiKeyApplication.created_at.desc())
    ).all()

    # 联查关联消费者的启停状态（消费者被停用时前端提示用户联系管理员）
    # 消费者行不存在（云端已删除并被 mirror 同步移除）时回带 deleted
    consumer_status_map: dict[str, str] = {}
    consumer_ids = {r.consumer_id for r in rows if r.consumer_id}
    if consumer_ids:
        consumer_rows = db.exec(
            select(ApiKeyConsumer).where(ApiKeyConsumer.tenant_id == tenant_id)
        ).all()
        consumer_status_map = {
            c.external_consumer_id: c.status
            for c in consumer_rows
            if c.external_consumer_id
        }

    return [
        _read(
            row,
            include_secret=True,
            consumer_status=(
                consumer_status_map.get(row.consumer_id or "")
                if row.consumer_id and row.consumer_id in consumer_status_map
                else ("deleted" if row.status == "approved" and row.consumer_id else None)
            ),
        )
        for row in rows
    ]


@router.get("/mine/usage", response_model=list[ApiKeyApplicationUsageItem])
def list_my_usage(
    tenant_id: str = Query(...),
    refresh: bool = Query(False, description="跳过 Redis 用量缓存，强制回源阿里云"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ApiKeyApplicationUsageItem]:
    """当前用户自己的配额使用情况（普通用户可查，本期=当前月）。

    数据源：当前用户 approved 申请记录 → 当月实时查阿里云并写入快照（upsert）；
    阿里云查询失败时回退读当月快照，保证换配额规则后已用量不丢。
    """
    ensure_tenant(db, tenant_id)

    rows = db.exec(
        select(ApiKeyApplication).where(
            ApiKeyApplication.tenant_id == tenant_id,
            ApiKeyApplication.user_id == current_user.id,
            ApiKeyApplication.status == "approved",
        )
    ).all()
    if not rows:
        return []

    month = _current_month()
    client = get_apig_client()

    # 组粒度规则（subjectType=consumer_group，2.1.23）：用量主体是消费组 csg-
    # 而非消费者 cs-，实时查询需换用消费者归属的组 ID；组内成员共享规则限额
    rule_subject_map: dict[str, str] = {}  # quota_rule_id -> subject_type
    rule_ids = {r.quota_rule_id for r in rows if r.quota_rule_id}
    if rule_ids:
        for rr in db.exec(
            select(ApiKeyQuotaRule).where(
                ApiKeyQuotaRule.tenant_id == tenant_id,
                ApiKeyQuotaRule.external_rule_id.in_(rule_ids),
            )
        ).all():
            if rr.external_rule_id:
                rule_subject_map[rr.external_rule_id] = rr.subject_type or "consumer"

    # 消费者 -> 归属消费组（组粒度用量查询的映射依据）
    consumer_group_map: dict[str, tuple[str | None, str | None]] = {}
    consumer_ids_all = {r.consumer_id for r in rows if r.consumer_id}
    if consumer_ids_all:
        for c in db.exec(
            select(ApiKeyConsumer).where(ApiKeyConsumer.tenant_id == tenant_id)
        ).all():
            if c.external_consumer_id:
                consumer_group_map[c.external_consumer_id] = (
                    c.external_consumer_group_id,
                    c.consumer_group_name,
                )

    items: list[ApiKeyApplicationUsageItem] = []
    for row in rows:
        quota_limit = int(row.quota_limit or 0)
        used_amount = 0
        rule_subject_type = rule_subject_map.get(row.quota_rule_id or "", "consumer")
        subject_id = row.consumer_id
        subject_name = row.consumer_name
        if (
            rule_subject_type == "consumer_group"
            and row.consumer_id
            and row.consumer_id in consumer_group_map
        ):
            group_id, group_name = consumer_group_map[row.consumer_id]
            if group_id:
                subject_id = group_id
                subject_name = group_name or group_id
        usage_cache_key = (
            f"{tenant_id}:usage:{row.gateway_id}:{row.quota_rule_id}:{subject_id}"
            if (row.gateway_id and row.quota_rule_id and subject_id)
            else None
        )
        cached_used = get_json(usage_cache_key) if (usage_cache_key and not refresh) else None
        if client and row.gateway_id and row.quota_rule_id and subject_id:
            if cached_used is not None:
                # TTL 内的用量缓存：跳过云端调用（快照照常 upsert，保持取较大值语义）
                used_amount = int(cached_used or 0)
            else:
                try:
                    resp = client.get_consumer_quota_usage(
                        gateway_id=row.gateway_id,
                        rule_id=row.quota_rule_id,
                        consumer_id=subject_id,
                    )
                    data = resp.get("data") if isinstance(resp, dict) else {}
                    used_amount = int(data.get("usedAmount") or 0)
                    if usage_cache_key:
                        set_json(usage_cache_key, used_amount)
                except (AliyunApigError, RuntimeError, ValueError):
                    used_amount = None  # 标记查询失败，回退快照
            if used_amount is not None:
                _upsert_usage_snapshot(
                    db,
                    tenant_id=tenant_id,
                    month=month,
                    consumer_id=subject_id,
                    consumer_name=subject_name,
                    gateway_id=row.gateway_id,
                    gateway_name=row.gateway_name,
                    quota_rule_id=row.quota_rule_id,
                    quota_rule_name=row.quota_rule_name,
                    quota_limit=row.quota_limit or 0,
                    quota_period=row.quota_period,
                    used_amount=used_amount,
                )
                db.commit()

        # 实时查询失败时，回退读当月快照（按规则叠加；组粒度主体查组 ID 的快照）
        if used_amount is None or not (client and row.gateway_id and row.quota_rule_id and subject_id):
            snap_rows = db.exec(
                select(ApiKeyUsageSnapshot).where(
                    ApiKeyUsageSnapshot.tenant_id == tenant_id,
                    ApiKeyUsageSnapshot.month == month,
                    ApiKeyUsageSnapshot.consumer_id == subject_id,
                )
            ).all() if subject_id else []
            if snap_rows:
                quota_limit = sum(int(s.quota_limit or 0) for s in snap_rows)
                used_amount = sum(int(s.used_amount or 0) for s in snap_rows)
            else:
                used_amount = 0

        usage_rate = (used_amount / quota_limit) if quota_limit > 0 else 0.0
        items.append(
            ApiKeyApplicationUsageItem(
                id=row.id,
                user_id=row.user_id,
                username=row.username,
                consumer_id=row.consumer_id,
                consumer_name=row.consumer_name,
                gateway_name=row.gateway_name,
                gateway_id=row.gateway_id,
                quota_limit=quota_limit,
                quota_period=row.quota_period,
                quota_rule_id=row.quota_rule_id,
                used_amount=used_amount,
                usage_rate=round(usage_rate, 4),
                suggestion=_usage_suggestion(usage_rate),
            )
        )
    return items


@router.delete("/mine/{application_id}", status_code=204)
def delete_my_application(
    application_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> None:
    """用户删除自己名下的终止态申请记录（已驳回/已吊销，或消费者已被删除的批准记录）。

    仅允许删除不再占用申请名额的记录：pending / 有效 approved（消费者仍在服务）
    不可删——前者是流程中记录，后者代表可用的 API Key。
    """
    ensure_tenant(db, tenant_id)
    row = db.get(ApiKeyApplication, application_id)
    if not row or row.tenant_id != tenant_id or row.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="申请记录不存在")
    if row.status == "pending":
        raise HTTPException(status_code=409, detail="待审批的申请不能删除")
    if row.status == "approved":
        # approved 仅当关联消费者已被删除（云端删除后同步移除）时才可清理；
        # 消费者仍在（或未关联）视为有效 Key，需先吊销
        consumer_alive = False
        if row.consumer_id:
            consumer_alive = (
                db.exec(
                    select(ApiKeyConsumer).where(
                        ApiKeyConsumer.tenant_id == tenant_id,
                        ApiKeyConsumer.external_consumer_id == row.consumer_id,
                    )
                ).first()
                is not None
            )
        if consumer_alive or not row.consumer_id:
            raise HTTPException(status_code=409, detail="该 API Key 仍在使用中，请先联系管理员吊销")
    db.delete(row)
    db.commit()


@router.get("", response_model=list[ApiKeyApplicationRead])
def list_applications(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ApiKeyApplicationRead]:
    ensure_tenant_admin(tenant_id, current_user)
    rows = db.exec(
        select(ApiKeyApplication).where(ApiKeyApplication.tenant_id == tenant_id)
    ).all()
    rows.sort(key=lambda r: (_STATUS_ORDER.get(r.status, 9), -r.created_at.timestamp()))

    # 回带消费者状态（enabled/disabled/deleted）——云端删除消费者并同步后，
    # 本地行被移除，approved 申请需向管理员展示「消费者已删除」
    consumer_status_map: dict[str, str] = {
        c.external_consumer_id: c.status
        for c in db.exec(
            select(ApiKeyConsumer).where(ApiKeyConsumer.tenant_id == tenant_id)
        ).all()
        if c.external_consumer_id
    }
    return [
        _read(
            row,
            include_secret=False,
            consumer_status=(
                consumer_status_map.get(row.consumer_id or "")
                if row.consumer_id and row.consumer_id in consumer_status_map
                else ("deleted" if row.status == "approved" and row.consumer_id else None)
            ),
        )
        for row in rows
    ]


def _get_pending(db: Session, application_id: str, tenant_id: str) -> ApiKeyApplication:
    row = db.get(ApiKeyApplication, application_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="API Key 申请不存在")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="该申请已处理，无法重复操作")
    return row


@router.post("/{application_id}/approve", response_model=ApiKeyApplicationRead)
def approve_application(
    application_id: str,
    request: ApiKeyApplicationApprove,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    row = _get_pending(db, application_id, request.tenant_id)

    consumer_name = (request.consumer_name or "").strip()
    if not consumer_name:
        raise HTTPException(status_code=400, detail="消费者名称不能为空")

    # 1) 解析选定的消费组与配额规则（传来的都是云端 ID，实时向阿里云校验）
    client = _require_apig_client("分配 API Key")
    gateways = get_gateway_configs()
    if not gateways:
        raise HTTPException(status_code=400, detail="后端未配置网关（ALIYUN_APIG_GATEWAYS）")
    gateway = gateways[0]
    try:
        group_detail = client.get_consumer_group(request.consumer_group_id)
        rule_detail = client.get_quota_rule(
            request.quota_rule_id, gateway_id=gateway.gateway_id
        )
    except (AliyunApigError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"消费组或配额规则无效：{exc}") from exc
    csg_name = group_detail.get("name") or ""
    rule_name = rule_detail.get("ruleName") or ""
    quota_limit = int(rule_detail.get("quotaLimit") or 0)
    period_type = rule_detail.get("periodType") or "day"

    # 2) 服务端生成强随机 API Key，以 Custom 模式创建消费者
    #    （凭证来源 Authorization: Bearer；实测云端 System 模式不回带明文，
    #    需要在本系统内展示/管理明文的场景必须由服务端生成并持有）
    api_key = _generate_api_key()
    # 消费者描述用 users 表的 display_name@username（display_name 缺失时回退 username）
    applicant = db.get(User, row.user_id) if row.user_id else None
    if applicant is None and row.username:
        applicant = db.exec(
            select(User).where(
                User.tenant_id == request.tenant_id,
                User.username == row.username,
            )
        ).first()
    applicant_username = (applicant.username if applicant else None) or row.username or row.user_id
    applicant_display = (
        (applicant.display_name or "").strip() if applicant else ""
    ) or applicant_username
    try:
        consumer_id = client.create_consumer(
            name=consumer_name,
            api_key=api_key,
            description=f"{applicant_display}@{applicant_username}",
        )
    except (AliyunApigError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"阿里云创建消费者失败：{exc}") from exc
    # 云端新增了消费者：清除节流/缓存标记，让消费组/消费者列表立即可见
    invalidate_tenant(request.tenant_id)

    # 3) 绑定消费组（batch-add） + 绑定配额规则
    # 配额规则两种接入方式：
    # - 规则本身是消费组粒度（subjectType=consumer_group，2.1.23 支持）：组成员
    #   入组即共享组规则限额，消费者无需（也不能）单独 addIds 绑定
    # - 规则是消费者粒度：逐个把新消费者 addIds 绑到规则
    # 判定方式：查规则当前 subjects，若存在 consumer_group 主体即认定组粒度规则
    rule_is_group_scoped = False
    try:
        for subj in client.list_quota_rule_subjects(
            request.quota_rule_id, gateway_id=gateway.gateway_id
        ):
            if subj.get("subjectType") == "consumer_group":
                rule_is_group_scoped = True
                break
    except (AliyunApigError, RuntimeError):
        rule_is_group_scoped = False  # 查询失败按消费者粒度处理（兼容旧行为）
    try:
        client.add_consumers_to_group(request.consumer_group_id, [consumer_id])
        if not rule_is_group_scoped:
            client.attach_consumer_to_rule(
                gateway_id=gateway.gateway_id,
                rule_id=request.quota_rule_id,
                consumer_id=consumer_id,
            )
    except (AliyunApigError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"阿里云绑定失败：{exc}") from exc

    # 3.5) 授权：组级授权对组内成员生效（实测 2026-09-03），组已有授权时
    #      无需单独授权；仅组未授权目标 API 时才为消费者兜底建个人规则
    try:
        client.ensure_group_member_authorized(
            consumer_id, request.consumer_group_id, gateway.gateway_id
        )
    except (AliyunApigError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"阿里云创建授权规则失败：{exc}") from exc

    # 4) 写透本地消费者表（供消费组页展示与启停管理）
    now = utc_now()
    existing_consumer = db.exec(
        select(ApiKeyConsumer).where(
            ApiKeyConsumer.tenant_id == request.tenant_id,
            ApiKeyConsumer.external_consumer_id == consumer_id,
        )
    ).first()
    if existing_consumer:
        local_consumer = existing_consumer
        local_consumer.external_consumer_group_id = request.consumer_group_id
        local_consumer.consumer_group_name = csg_name
        local_consumer.updated_at = now
    else:
        local_consumer = ApiKeyConsumer(
            tenant_id=request.tenant_id,
            name=consumer_name,
            description=f"审批创建 · 申请人 {row.username or row.user_id}",
            gateway_id=gateway.gateway_id,
            gateway_name=gateway.name,
            external_consumer_id=consumer_id,
            external_consumer_group_id=request.consumer_group_id,
            consumer_group_name=csg_name,
            consumer_type="AI",
            enable=True,
            status="enabled",
            created_by_user_id=current_user.id,
            created_at=now,
            updated_at=now,
        )
        db.add(local_consumer)

    # 5) 落库审批单
    default_api_url = "https://ai-gateway.folidaymall.com/v1/chat/completions"
    row.api_key_encrypted = encrypt_secret(api_key)
    row.api_url = (request.api_url or "").strip() or default_api_url
    row.gateway_name = gateway.name
    row.gateway_id = gateway.gateway_id
    row.quota_limit = quota_limit
    row.quota_period = period_type
    row.quota_rule_id = request.quota_rule_id
    row.quota_rule_name = rule_name
    row.consumer_id = consumer_id
    row.consumer_name = consumer_name
    row.consumer_group_id = request.consumer_group_id
    row.consumer_group_name = csg_name
    row.status = "approved"
    row.reviewer_user_id = current_user.id
    row.reviewer_note = None
    row.reviewed_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row, include_secret=False)


@router.post("/{application_id}/reject", response_model=ApiKeyApplicationRead)
def reject_application(
    application_id: str,
    request: ApiKeyApplicationReview,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    row = _get_pending(db, application_id, request.tenant_id)
    row.status = "rejected"
    row.reviewer_user_id = current_user.id
    row.reviewer_note = (request.reviewer_note or "").strip() or None
    row.reviewed_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row, include_secret=False)


def _consumer_exists(client: Any, consumer_id: str) -> bool:
    """检查消费者在云端是否仍存在（GetConsumer，任何失败都按不存在处理）。"""
    try:
        client.get_consumer(consumer_id)
    except (AliyunApigError, RuntimeError, ValueError):
        return False
    return True


def _list_consumer_api_keys(client: Any, consumer_id: str) -> list[str]:
    """尽力拉取消费者名下的 API Key 明文列表（GetConsumer credentials）。

    实测（2026-09）：真实云端在凭证以正确结构写入后 GetConsumer 会回带明文
    （credentials 挂在 apiKeyIdentityConfig 顶层）；mock 模式同样回带。
    """
    try:
        detail = client.get_consumer(consumer_id)
    except (AliyunApigError, RuntimeError, ValueError):
        return []
    identity = detail.get("apiKeyIdentityConfig") or detail.get("apikeyIdentityConfig") or {}
    keys: list[str] = []
    # 新结构：credentials 挂在 apiKeyIdentityConfig 顶层（实测有效）
    for cred in identity.get("credentials") or []:
        ak = cred.get("apikey") or cred.get("apiKey")
        if ak and ak not in keys:
            keys.append(ak)
    # 兼容旧结构：credentials 嵌在 apiKeySources[] 内
    for src in identity.get("apiKeySources", []) + identity.get("apikeySources", []):
        for cred in src.get("credentials") or []:
            ak = cred.get("apikey") or cred.get("apiKey")
            if ak and ak not in keys:
                keys.append(ak)
    return keys


@router.get("/{application_id}/revoke-preview")
def revoke_preview(
    application_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """吊销前预检：判定该消费者名下凭证数量，前端据此展示确认文案。"""
    ensure_tenant_admin(tenant_id, current_user)
    row = db.get(ApiKeyApplication, application_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="API Key 申请不存在")
    if row.status != "approved":
        raise HTTPException(status_code=409, detail="仅已分配的 API Key 可吊销")
    if not row.consumer_id:
        return {"api_key_count": 1, "will_disable_consumer": False, "consumer_name": row.consumer_name}

    client = get_apig_client()
    keys = _list_consumer_api_keys(client, row.consumer_id) if client else []
    if not keys:
        # 云端不回带凭证 → 按本地同消费者 approved 记录数估算
        sibling_count = len(
            db.exec(
                select(ApiKeyApplication).where(
                    ApiKeyApplication.tenant_id == tenant_id,
                    ApiKeyApplication.status == "approved",
                    ApiKeyApplication.consumer_id == row.consumer_id,
                )
            ).all()
        )
        key_count = max(sibling_count, 1)
    else:
        key_count = len(keys)
    return {
        "api_key_count": key_count,
        "will_disable_consumer": key_count <= 1,
        "consumer_name": row.consumer_name,
    }


@router.post("/{application_id}/revoke", response_model=ApiKeyApplicationRead)
def revoke_application(
    application_id: str,
    request: ApiKeyApplicationReview,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    """吊销已分配的 API Key：去阿里云删除消费者下属对应凭证。

    - 消费者仅剩这一个凭证时：停用（disable）该消费者，避免云端删除整消费者影响他人；
    - 存在多个凭证时：用剩余凭证覆盖写（云侧无删除单凭证 API），消费者继续可用。
    """
    ensure_tenant_admin(request.tenant_id, current_user)
    row = db.get(ApiKeyApplication, application_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="API Key 申请不存在")
    if row.status == "revoked":
        return _read(row, include_secret=False)
    if row.status != "approved":
        raise HTTPException(status_code=409, detail="仅已分配的 API Key 可吊销")

    revoked_key = try_decrypt_secret(row.api_key_encrypted or "")
    client = get_apig_client()

    # 消费者已在云端被删除（如管理员手动删除后同步）时跳过云端调用，
    # 直接做本地落库——云端 UpdateConsumer 会 404 导致吊销卡死。
    consumer_exists = (
        bool(client) and bool(row.consumer_id) and _consumer_exists(client, row.consumer_id)
    )

    if client and row.consumer_id and consumer_exists:
        keys = _list_consumer_api_keys(client, row.consumer_id)
        try:
            if not keys:
                # 云端不回带凭证：按本地同消费者剩余记录推断
                remaining_keys = []
                siblings = db.exec(
                    select(ApiKeyApplication).where(
                        ApiKeyApplication.tenant_id == request.tenant_id,
                        ApiKeyApplication.status == "approved",
                        ApiKeyApplication.consumer_id == row.consumer_id,
                    )
                ).all()
                for s in siblings:
                    if s.id == row.id:
                        continue
                    k = try_decrypt_secret(s.api_key_encrypted or "")
                    if k:
                        remaining_keys.append(k)
                if remaining_keys:
                    # 多凭证：覆盖写剩余凭证（移除被吊销的 Key）
                    client.update_consumer(row.consumer_id, keep_credentials=remaining_keys)
                else:
                    # 单凭证：停用消费者
                    client.update_consumer(row.consumer_id, enable=False)
                    local = db.exec(
                        select(ApiKeyConsumer).where(
                            ApiKeyConsumer.tenant_id == request.tenant_id,
                            ApiKeyConsumer.external_consumer_id == row.consumer_id,
                        )
                    ).first()
                    if local:
                        local.enable = False
                        local.status = "disabled"
                        local.updated_at = utc_now()
                        db.add(local)
            elif revoked_key and revoked_key in keys:
                if len(keys) <= 1:
                    client.update_consumer(row.consumer_id, enable=False)
                    local = db.exec(
                        select(ApiKeyConsumer).where(
                            ApiKeyConsumer.tenant_id == request.tenant_id,
                            ApiKeyConsumer.external_consumer_id == row.consumer_id,
                        )
                    ).first()
                    if local:
                        local.enable = False
                        local.status = "disabled"
                        local.updated_at = utc_now()
                        db.add(local)
                else:
                    remaining = [k for k in keys if k != revoked_key]
                    client.update_consumer(row.consumer_id, keep_credentials=remaining)
            else:
                # 找不到匹配凭证（Key 已在云端被轮换等），保守起见仅停用
                client.update_consumer(row.consumer_id, enable=False)
                local = db.exec(
                    select(ApiKeyConsumer).where(
                        ApiKeyConsumer.tenant_id == request.tenant_id,
                        ApiKeyConsumer.external_consumer_id == row.consumer_id,
                    )
                ).first()
                if local:
                    local.enable = False
                    local.status = "disabled"
                    local.updated_at = utc_now()
                    db.add(local)
        except (AliyunApigError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=f"阿里云吊销失败：{exc}") from exc

    row.status = "revoked"
    row.api_key_encrypted = None
    row.api_url = None
    row.reviewer_user_id = current_user.id
    row.reviewer_note = (request.reviewer_note or "").strip() or None
    row.reviewed_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row, include_secret=False)


def _current_month() -> str:
    return utc_now().strftime("%Y-%m")


def _upsert_usage_snapshot(
    db: Session,
    tenant_id: str,
    month: str,
    consumer_id: str,
    consumer_name: str | None,
    gateway_id: str | None,
    gateway_name: str | None,
    quota_rule_id: str,
    quota_rule_name: str | None,
    quota_limit: int,
    quota_period: str | None,
    used_amount: int,
) -> None:
    """把一次实时用量写入快照表（幂等 upsert，used_amount 取较大值）。"""
    if not consumer_id or not quota_rule_id:
        return
    row = db.exec(
        select(ApiKeyUsageSnapshot).where(
            ApiKeyUsageSnapshot.tenant_id == tenant_id,
            ApiKeyUsageSnapshot.month == month,
            ApiKeyUsageSnapshot.consumer_id == consumer_id,
            ApiKeyUsageSnapshot.quota_rule_id == quota_rule_id,
        )
    ).first()
    if row is None:
        db.add(
            ApiKeyUsageSnapshot(
                tenant_id=tenant_id,
                month=month,
                consumer_id=consumer_id,
                consumer_name=consumer_name,
                gateway_id=gateway_id,
                gateway_name=gateway_name,
                quota_rule_id=quota_rule_id,
                quota_rule_name=quota_rule_name,
                quota_limit=int(quota_limit or 0),
                quota_period=quota_period,
                used_amount=int(used_amount or 0),
            )
        )
    else:
        # 取较大值防止云端周期重置导致快照回退（月内用量单调递增）
        row.used_amount = max(int(row.used_amount or 0), int(used_amount or 0))
        row.consumer_name = consumer_name or row.consumer_name
        row.quota_rule_name = quota_rule_name or row.quota_rule_name
        row.quota_limit = int(quota_limit or row.quota_limit or 0)
        row.quota_period = quota_period or row.quota_period
        row.gateway_id = gateway_id or row.gateway_id
        row.gateway_name = gateway_name or row.gateway_name
        row.updated_at = utc_now()
        db.add(row)


def _summarize_snapshot_rows(rows: list[ApiKeyUsageSnapshot]) -> list[dict[str, Any]]:
    """把快照行按消费者聚合：quota_limit 求和（换规则叠加）、used_amount 求和。"""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for snap in rows:
        key = snap.consumer_id or snap.consumer_name or snap.id
        entry = merged.get(key)
        if entry is None:
            entry = {
                "consumer_id": snap.consumer_id,
                "consumer_name": snap.consumer_name or snap.consumer_id,
                "gateway_name": snap.gateway_name,
                "gateway_id": snap.gateway_id,
                "quota_limit": 0,
                "quota_period": snap.quota_period,
                "quota_rule_ids": [snap.quota_rule_id] if snap.quota_rule_id else [],
                "quota_rule_names": [snap.quota_rule_name] if snap.quota_rule_name else [],
                "used_amount": 0,
            }
            merged[key] = entry
            order.append(key)
        entry["quota_limit"] += int(snap.quota_limit or 0)
        entry["used_amount"] += int(snap.used_amount or 0)
        if snap.quota_period and not entry["quota_period"]:
            entry["quota_period"] = snap.quota_period
        if snap.quota_rule_id and snap.quota_rule_id not in entry["quota_rule_ids"]:
            entry["quota_rule_ids"].append(snap.quota_rule_id)
        if snap.quota_rule_name and snap.quota_rule_name not in entry["quota_rule_names"]:
            entry["quota_rule_names"].append(snap.quota_rule_name)
    return [merged[key] for key in order]


def _coerce_str(value: str | None) -> str | None:
    """兼容 FastAPI Query 默认值对象（直接调用函数时可能传入 Query 对象）。"""
    if value is None or isinstance(value, str):
        return value
    return getattr(value, "default", None)


def _derive_user_no(user_id: str | None) -> str | None:
    if not user_id:
        return None
    return f"{1000 + (hash(user_id) % 9000):04d}"


def _usage_suggestion(usage_rate: float) -> str:
    if usage_rate >= 0.9:
        return "expand"
    if usage_rate >= 0.7:
        return "watch"
    return "normal"


@router.get("/usage", response_model=ApiKeyApplicationUsageRead)
def list_usage(
    tenant_id: str = Query(...),
    month: str | None = Query(None, description="格式 YYYY-MM，缺省为当前月"),
    refresh: bool = Query(False, description="跳过 Redis 缓存/节流，强制回源阿里云"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationUsageRead:
    """配额使用明细：以消费者为主体，按月查询用量。

    - 当月：实时从阿里云 ListQuotaRuleSubjects 查询，并 upsert 写入快照表（api_key_usage_snapshots）；
    - 历史月份：直接读快照表，不再回源阿里云；
    - 消费者中途更换配额规则时，快照按规则维度叠加，换规则不丢当月已用量。
    """
    ensure_tenant_admin(tenant_id, current_user)
    target_month = (_coerce_str(month) or _current_month()).strip()
    current_month = _current_month()
    is_current_month = target_month >= current_month

    if is_current_month:
        # 仅当月查询时同步消费者（历史月不需要，避免多余阿里云调用）
        gateways = get_gateway_configs()
        if gateways:
            try:
                client = get_apig_client()
                if refresh or not acquire_sync_throttle(f"{tenant_id}:sync:consumers"):
                    _sync_consumers_to_local(db, tenant_id, client, gateways)
            except (AliyunApigError, RuntimeError):
                pass

    consumer_rows = db.exec(
        select(ApiKeyConsumer).where(ApiKeyConsumer.tenant_id == tenant_id).order_by(ApiKeyConsumer.name)
    ).all()

    client = get_apig_client()

    # 当月：实时查 subjects 构建 {subject_id: (rule, subj)} 映射并写快照。
    # 主体两种粒度（2.1.21+ 支持组粒度）：
    # - subjectType=consumer：subject id 为消费者 cs-，直接按消费者落快照；
    # - subjectType=consumer_group：subject id 为消费组 csg-，组内成员共享
    #   规则限额与用量。以「组」为主体落一条快照（配额/用量只计一次，
    #   避免 N 个成员重复计数）；个人面板 /mine/usage 会把组用量映射给成员。
    subject_usage: dict[str, tuple[ApiKeyQuotaRule, dict[str, Any]]] = {}
    if is_current_month and client:
        rule_rows = db.exec(
            select(ApiKeyQuotaRule).where(ApiKeyQuotaRule.tenant_id == tenant_id)
        ).all()
        for rule in rule_rows:
            if not rule.external_rule_id:
                continue
            # 规则主体列表短 TTL 缓存（JSON dict 列表，与云端结构一致）
            subjects_key = f"{tenant_id}:subjects:{rule.gateway_id}:{rule.external_rule_id}"
            subjects = get_json(subjects_key) if not refresh else None
            if subjects is None:
                try:
                    subjects = client.list_quota_rule_subjects(
                        rule.external_rule_id,
                        gateway_id=rule.gateway_id,
                    )
                except (AliyunApigError, RuntimeError, ValueError):
                    continue
                set_json(subjects_key, subjects)
            for subj in subjects if isinstance(subjects, list) else []:
                sid = subj.get("id") or ""
                if sid:
                    # 组主体补组名，快照/展示用（云端 subjectName 可能为空，回退本地组名）
                    if subj.get("subjectType") == "consumer_group" and not subj.get("name"):
                        grp = db.exec(
                            select(ApiKeyConsumerGroup).where(
                                ApiKeyConsumerGroup.tenant_id == tenant_id,
                                ApiKeyConsumerGroup.external_consumer_group_id == sid,
                            )
                        ).first()
                        if grp:
                            subj["name"] = grp.name
                    subject_usage[sid] = (rule, subj)

    # 构建当月快照（存量快照按 consumer 维度分组，实时数据 upsert 后叠加展示）
    snap_rows = db.exec(
        select(ApiKeyUsageSnapshot).where(
            ApiKeyUsageSnapshot.tenant_id == tenant_id,
            ApiKeyUsageSnapshot.month == target_month,
        )
    ).all()

    # 当月实时数据先 upsert 进快照（消费者主体按消费者行；组主体单独立行）
    if is_current_month and client:
        for row in consumer_rows:
            rule, subj = subject_usage.get(row.external_consumer_id or "", (None, {}))
            if rule is None:
                continue
            _upsert_usage_snapshot(
                db,
                tenant_id=tenant_id,
                month=target_month,
                consumer_id=row.external_consumer_id or "",
                consumer_name=row.name,
                gateway_id=row.gateway_id,
                gateway_name=row.gateway_name,
                quota_rule_id=rule.external_rule_id or "",
                quota_rule_name=rule.name,
                quota_limit=int(rule.quota_limit or 0),
                quota_period=rule.period_type,
                used_amount=int(subj.get("usedAmount") or 0),
            )
        # 组粒度主体（subjectType=consumer_group）：组共享规则限额与用量，以组
        # 为主体落一条快照，避免按组内每个消费者重复计配额；行内不与消费者行混淆
        for sid, (rule, subj) in subject_usage.items():
            if rule is None or subj.get("subjectType") != "consumer_group":
                continue
            _upsert_usage_snapshot(
                db,
                tenant_id=tenant_id,
                month=target_month,
                consumer_id=sid,
                consumer_name=subj.get("name") or sid,
                gateway_id=rule.gateway_id,
                gateway_name=rule.gateway_name,
                quota_rule_id=rule.external_rule_id or "",
                quota_rule_name=rule.name,
                quota_limit=int(rule.quota_limit or 0),
                quota_period=rule.period_type,
                used_amount=int(subj.get("usedAmount") or 0),
            )
        db.commit()
        db.expire_all()
        snap_rows = db.exec(
            select(ApiKeyUsageSnapshot).where(
                ApiKeyUsageSnapshot.tenant_id == tenant_id,
                ApiKeyUsageSnapshot.month == target_month,
            )
        ).all()

    # 按消费者聚合快照行（quota_limit 求和 → 换规则叠加，used_amount 求和）
    merged_by_consumer = _summarize_snapshot_rows(list(snap_rows))
    merged_map = {m["consumer_id"]: m for m in merged_by_consumer}

    items: list[ApiKeyApplicationUsageItem] = []
    total_quota = 0
    total_used = 0
    high_count = 0
    low_count = 0

    # 行主体 = 当前消费者列表（历史月份再并入快照中出现过的消费者，保留历史）；
    # 组粒度主体（csg- 行）追加在消费者行之后展示
    current_consumer_ids = {row.external_consumer_id or "" for row in consumer_rows}
    seen: set[str] = set()
    ordered_consumers: list[tuple[str, str | None, str | None, str | None]] = []
    for row in consumer_rows:
        cid = row.external_consumer_id or ""
        if cid not in seen:
            seen.add(cid)
            ordered_consumers.append((cid, row.name, row.gateway_name, row.gateway_id))
    for sid, (rule, subj) in subject_usage.items():
        if subj.get("subjectType") == "consumer_group" and sid not in seen:
            seen.add(sid)
            ordered_consumers.append((sid, subj.get("name") or sid, rule.gateway_name, rule.gateway_id))
    if not is_current_month:
        for m in merged_by_consumer:
            cid = m["consumer_id"] or ""
            if cid and cid not in seen:
                seen.add(cid)
                ordered_consumers.append(
                    (cid, m["consumer_name"], m["gateway_name"], m["gateway_id"])
                )

    for cid, name, gateway_name, gateway_id in ordered_consumers:
        m = merged_map.get(cid)
        if m:
            quota_limit = int(m["quota_limit"])
            used_amount = int(m["used_amount"])
            quota_period = m["quota_period"]
            rule_names = m["quota_rule_names"]
        else:
            rule, subj = subject_usage.get(cid, (None, {}))
            quota_limit = int(rule.quota_limit or 0) if rule else 0
            used_amount = int(subj.get("usedAmount") or 0)
            quota_period = rule.period_type if rule else None
            rule_names = [rule.name] if rule else []

        usage_rate = (used_amount / quota_limit) if quota_limit > 0 else 0.0
        suggestion = _usage_suggestion(usage_rate)
        if usage_rate >= 0.9:
            high_count += 1
        elif usage_rate <= 0.4:
            low_count += 1

        total_quota += quota_limit
        total_used += used_amount

        items.append(
            ApiKeyApplicationUsageItem(
                id=cid or name or "",
                user_id=cid or "",
                username=name,
                user_no=None,
                department=None,
                consumer_id=cid or None,
                consumer_name=name,
                gateway_name=gateway_name,
                gateway_id=gateway_id,
                quota_limit=quota_limit,
                quota_period=quota_period,
                quota_rule_id=", ".join(rule_names) or None,
                used_amount=used_amount,
                usage_rate=round(usage_rate, 4),
                suggestion=suggestion,
            )
        )

    allocated_users = len(items)
    avg_usage_rate = (total_used / total_quota) if total_quota > 0 else 0.0

    return ApiKeyApplicationUsageRead(
        month=target_month,
        summary=ApiKeyUsageSummary(
            allocated_users=allocated_users,
            total_quota=total_quota,
            total_used=total_used,
            avg_usage_rate=round(avg_usage_rate, 4),
            high_watermark_users=high_count,
            low_watermark_users=low_count,
        ),
        items=items,
    )


@router.post("/{application_id}/quota", response_model=ApiKeyApplicationRead)
def update_quota(
    application_id: str,
    request: ApiKeyQuotaUpdate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    """手动调整某条 API Key 的 Token 配额（管理员）。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    row = db.get(ApiKeyApplication, application_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="API Key 申请不存在")
    if row.status != "approved":
        raise HTTPException(status_code=409, detail="仅已批准的 API Key 可调整配额")
    if not isinstance(request.quota_limit, int) or request.quota_limit <= 0:
        raise HTTPException(status_code=400, detail="quota_limit 必须为正整数")

    client = get_apig_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="后端未配置阿里云 AK/SK，无法调整配额",
        )

    if row.gateway_id and row.quota_rule_id:
        try:
            client.update_consumer_quota_rule(
                gateway_id=row.gateway_id,
                rule_id=row.quota_rule_id,
                quota_limit=request.quota_limit,
            )
        except (AliyunApigError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    row.quota_limit = int(request.quota_limit)
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row, include_secret=False)
