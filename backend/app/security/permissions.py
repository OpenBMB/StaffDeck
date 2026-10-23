from __future__ import annotations

from fastapi import Depends, HTTPException, Query
from sqlmodel import Session

from app.db import get_session
from app.db.models import AgentProfile, User
from app.security.auth import ensure_current_user_tenant, get_current_user

ADMIN_ROLE = "admin"
MEMBER_ROLE = "member"
USER_ROLES = {ADMIN_ROLE, MEMBER_ROLE}

# 账号来源标记（users.source）：
#   web   = 网页端注册/管理员创建的租户真实成员（含域账号登录同步）
#   share = 分享链接免登录访客，首次打开链接时懒建，不是租户成员
USER_SOURCE_WEB = "web"
SHARE_GUEST_SOURCE = "share"
GUEST_USER_SOURCES = frozenset({SHARE_GUEST_SOURCE})


def is_admin_user(current_user: User) -> bool:
    return current_user.role == ADMIN_ROLE


def ensure_tenant_admin(tenant_id: str, current_user: User) -> User:
    ensure_current_user_tenant(tenant_id, current_user)
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only administrator can manage tenant settings")
    return current_user


def require_tenant_admin(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
) -> User:
    return ensure_tenant_admin(tenant_id, current_user)


def require_agent_scope_viewer(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> User:
    ensure_current_user_tenant(tenant_id, current_user)
    if not agent_id:
        return current_user
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    if (
        is_admin_user(current_user)
        or row.is_overall
        or agent_owned_by_user(row, current_user)
        or (row.metadata_json or {}).get("published_to_gallery") is True
    ):
        return current_user
    raise HTTPException(status_code=403, detail="Cannot access this staff")


def ensure_open_gallery_admin(tenant_id: str, current_user: User) -> None:
    ensure_tenant_admin(tenant_id, current_user)


def ensure_open_gallery_member(tenant_id: str, current_user: User) -> User:
    """Open-gallery writes (general_skill) are open to every member of the tenant.

    Decoupling the open gallery from admin-only publishing: any tenant member may
    import/publish a general skill into the open gallery. Retained separately from
    ``ensure_open_gallery_admin`` because the other resource types (skill/knowledge/
    tool) still require admin.
    """
    ensure_current_user_tenant(tenant_id, current_user)
    return current_user


def ensure_agent_scope_manager(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    current_user: User,
) -> AgentProfile | None:
    ensure_current_user_tenant(tenant_id, current_user)
    if not agent_id:
        return None
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    if is_admin_user(current_user):
        return row
    if row.is_overall:
        raise HTTPException(status_code=403, detail="Only administrator can manage overall agent")
    if agent_owned_by_user(row, current_user):
        return row
    raise HTTPException(status_code=403, detail="Only the creator or administrator can manage this staff")


def agent_owned_by_user(row: AgentProfile, user: User) -> bool:
    metadata = row.metadata_json or {}
    return metadata.get("owner_user_id") == user.id
