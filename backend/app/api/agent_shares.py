"""数字员工分享链接。

站内用户创建分享链接（锁定模型 + 有效期），访客凭 URL 免登录打开对话窗；
访客身份落成一条真实的 `users` 行（`source="share"`），因此会话隔离直接复用
现有的 `sessions.user_id` 过滤，无需改动会话查询逻辑。

访客账号不是租户成员：`GET /api/auth/users` 默认按 `source == "web"` 过滤，
访客不会混进用户管理列表与处理人选择器（常量见 app.security.permissions）。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import (
    AgentProfile,
    AgentShareLink,
    AgentShareVisitor,
    ModelConfig,
    User,
    new_id,
    utc_now,
)
from app.security.auth import create_share_access_token, get_current_user
from app.security.permissions import (
    SHARE_GUEST_SOURCE,
    agent_owned_by_user,
    is_admin_user,
)
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/agent-shares", tags=["agent-shares"])
public_router = APIRouter(prefix="/api/public/agent-shares", tags=["agent-shares:public"])

# 有效期档位（分钟 -> 时长；None = 永久）
SHARE_TTL_OPTIONS: dict[str, timedelta | None] = {
    "30m": timedelta(minutes=30),
    "2h": timedelta(hours=2),
    "forever": None,
}

ShareTtl = Literal["30m", "2h", "forever"]


class AgentShareCreateRequest(BaseModel):
    tenant_id: str
    agent_id: str
    model_config_id: str
    ttl: ShareTtl = "2h"


class AgentShareRead(BaseModel):
    id: str
    token: str
    agent_id: str
    model_config_id: str
    expires_at: Optional[datetime] = None
    created_at: datetime


class AgentSharePublicInfo(BaseModel):
    """访客可见的分享元信息：只暴露展示所需字段，不带租户内部数据。"""

    token: str
    agent_id: str
    agent_name: str
    agent_description: Optional[str] = None
    model_label: str
    expires_at: Optional[datetime] = None


class AgentShareVisitorSessionRequest(BaseModel):
    visitor_key: Optional[str] = None


class AgentShareVisitorUser(BaseModel):
    id: str
    tenant_id: str
    username: str
    display_name: Optional[str] = None
    role: str


class AgentShareVisitorSessionRead(BaseModel):
    access_token: str
    visitor_key: str
    agent_id: str
    model_config_id: str
    expires_at: Optional[datetime] = None
    user: AgentShareVisitorUser


def _require_shareable_agent(
    db: Session,
    tenant_id: str,
    agent_id: str,
    current_user: User,
) -> AgentProfile:
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id or row.status != "active" or row.is_overall:
        raise HTTPException(status_code=404, detail="Agent not available")
    metadata = row.metadata_json or {}
    visible = (
        is_admin_user(current_user)
        or agent_owned_by_user(row, current_user)
        or metadata.get("published_to_gallery") is True
    )
    # 口径与对话页一致：能在站内跟这个数字员工对话，就能把它分享出去
    if not visible:
        raise HTTPException(status_code=403, detail="Agent not available")
    return row


def _require_enabled_model(db: Session, tenant_id: str, model_config_id: str) -> ModelConfig:
    row = db.get(ModelConfig, model_config_id)
    if not row or row.tenant_id != tenant_id or not row.enabled:
        raise HTTPException(status_code=404, detail="Model not available")
    return row


def resolve_share_link(db: Session, token: str) -> AgentShareLink:
    """按 token 取分享链接并校验有效性（不存在 / 已撤销 / 已过期）。"""
    row = db.exec(select(AgentShareLink).where(AgentShareLink.token == token)).first()
    if not row or row.revoked_at is not None:
        raise HTTPException(status_code=404, detail="分享链接不存在或已失效")
    if row.expires_at is not None and row.expires_at <= utc_now():
        raise HTTPException(status_code=410, detail="分享链接已过期")
    return row


def _model_label(model: ModelConfig | None) -> str:
    if model is None:
        return ""
    return str(getattr(model, "name", None) or getattr(model, "model", "") or model.id)


@router.post("", response_model=AgentShareRead)
def create_agent_share(
    request: AgentShareCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AgentShareRead:
    if request.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, request.tenant_id)
    _require_shareable_agent(db, request.tenant_id, request.agent_id, current_user)
    _require_enabled_model(db, request.tenant_id, request.model_config_id)

    ttl = SHARE_TTL_OPTIONS[request.ttl]
    row = AgentShareLink(
        id=new_id("agentshare"),
        token=secrets.token_urlsafe(24),
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        model_config_id=request.model_config_id,
        created_by=current_user.id,
        expires_at=None if ttl is None else utc_now() + ttl,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return AgentShareRead(
        id=row.id,
        token=row.token,
        agent_id=row.agent_id,
        model_config_id=row.model_config_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


@public_router.get("/{token}", response_model=AgentSharePublicInfo)
def read_agent_share(token: str, db: Session = Depends(get_session)) -> AgentSharePublicInfo:
    link = resolve_share_link(db, token)
    agent = db.get(AgentProfile, link.agent_id)
    if not agent or agent.status != "active":
        raise HTTPException(status_code=404, detail="数字员工已下线")
    return AgentSharePublicInfo(
        token=link.token,
        agent_id=link.agent_id,
        agent_name=agent.name,
        agent_description=agent.description,
        model_label=_model_label(db.get(ModelConfig, link.model_config_id)),
        expires_at=link.expires_at,
    )


@public_router.post("/{token}/session", response_model=AgentShareVisitorSessionRead)
def open_agent_share_session(
    token: str,
    payload: AgentShareVisitorSessionRequest,
    db: Session = Depends(get_session),
) -> AgentShareVisitorSessionRead:
    """用浏览器持有的 visitor_key 换一个受限访问令牌；没带或对不上就发一个新的访客身份。

    visitor_key 由服务端随机签发并只在响应里出现过一次，因此不可被他人猜测冒用。
    """
    link = resolve_share_link(db, token)
    agent = db.get(AgentProfile, link.agent_id)
    if not agent or agent.status != "active":
        raise HTTPException(status_code=404, detail="数字员工已下线")
    model = db.get(ModelConfig, link.model_config_id)
    if not model or not model.enabled:
        raise HTTPException(status_code=404, detail="分享锁定的模型已停用")

    visitor: AgentShareVisitor | None = None
    if payload.visitor_key:
        candidate = db.exec(
            select(AgentShareVisitor).where(AgentShareVisitor.visitor_key == payload.visitor_key)
        ).first()
        # visitor_key 只在同一条分享内复用，避免跨分享串上下文
        if candidate is not None and candidate.share_id == link.id:
            visitor = candidate

    if visitor is None:
        guest = User(
            id=new_id("guest"),
            tenant_id=link.tenant_id,
            username=f"share_guest_{secrets.token_hex(6)}",
            display_name="分享访客",
            role="member",
            source=SHARE_GUEST_SOURCE,
            password_hash="",
        )
        db.add(guest)
        db.flush()
        visitor = AgentShareVisitor(
            id=new_id("sharevisitor"),
            share_id=link.id,
            tenant_id=link.tenant_id,
            user_id=guest.id,
            visitor_key=secrets.token_urlsafe(24),
        )
        db.add(visitor)
    else:
        guest = db.get(User, visitor.user_id)
        if guest is None or guest.tenant_id != link.tenant_id:
            raise HTTPException(status_code=410, detail="访客身份已失效")
        visitor.last_seen_at = utc_now()
        db.add(visitor)

    link.access_count += 1
    link.last_access_at = utc_now()
    db.add(link)
    db.commit()
    db.refresh(visitor)
    db.refresh(guest)

    remaining_ttl: int | None = None
    if link.expires_at is not None:
        remaining_ttl = int((link.expires_at - utc_now()).total_seconds())
    access_token = create_share_access_token(
        guest,
        share_id=link.id,
        share_token=link.token,
        agent_id=link.agent_id,
        model_config_id=link.model_config_id,
        ttl_seconds=remaining_ttl,
    )

    return AgentShareVisitorSessionRead(
        access_token=access_token,
        visitor_key=visitor.visitor_key,
        agent_id=link.agent_id,
        model_config_id=link.model_config_id,
        expires_at=link.expires_at,
        user=AgentShareVisitorUser(
            id=guest.id,
            tenant_id=guest.tenant_id,
            username=guest.username,
            display_name=guest.display_name,
            role=guest.role,
        ),
    )
