from __future__ import annotations

from fastapi import Depends, HTTPException, Query
from sqlmodel import Session

from app.db import get_session
from app.db.models import AgentProfile, User
from app.security.auth import ensure_current_user_tenant, get_current_user

ADMIN_ROLE = "admin"
MEMBER_ROLE = "member"
USER_ROLES = {ADMIN_ROLE, MEMBER_ROLE}


def is_admin_user(current_user: User) -> bool:
    return current_user.role == ADMIN_ROLE


def ensure_tenant_admin(tenant_id: str, current_user: User) -> User:
    ensure_current_user_tenant(tenant_id, current_user)
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only administrator can manage tenant settings")
    from app.security.module_policy import require_resource
    from staffdeck_harness.contracts.security import ResourceRef

    require_resource(current_user, ResourceRef(type="tenant", id=tenant_id, tenant_id=tenant_id), "manage", module="tenant")
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
        from app.security.module_policy import require_resource
        from staffdeck_harness.composition.projection import agent_ref

        require_resource(current_user, agent_ref(row), "view", module="staff")
        return current_user
    raise HTTPException(status_code=403, detail="Cannot access this staff")


def ensure_open_gallery_admin(tenant_id: str, current_user: User) -> None:
    ensure_tenant_admin(tenant_id, current_user)


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
        from app.security.module_policy import require_resource
        from staffdeck_harness.composition.projection import agent_ref

        require_resource(current_user, agent_ref(row), "manage", module="staff")
        return row
    if row.is_overall:
        raise HTTPException(status_code=403, detail="Only administrator can manage overall agent")
    if agent_owned_by_user(row, current_user):
        from app.security.module_policy import require_resource
        from staffdeck_harness.composition.projection import agent_ref

        require_resource(current_user, agent_ref(row), "manage", module="staff")
        return row
    raise HTTPException(status_code=403, detail="Only the creator or administrator can manage this staff")


def agent_owned_by_user(row: AgentProfile, user: User) -> bool:
    metadata = row.metadata_json or {}
    return metadata.get("owner_user_id") == user.id
