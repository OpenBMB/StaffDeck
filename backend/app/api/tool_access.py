from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.agents.branching import (
    ensure_private_resource_binding,
    get_agent,
    get_overall_agent,
    is_open_gallery_resource,
    resource_binding_metadata,
)
from app.db import get_session
from app.db.models import (
    AgentResourceBinding,
    Tool,
    ToolAccessRequest,
    User,
    UserNotification,
    utc_now,
)
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import ensure_agent_scope_manager
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/enterprise/tool-access-requests", tags=["enterprise:tool-access"])
notifications_router = APIRouter(
    prefix="/api/enterprise/notifications", tags=["enterprise:notifications"]
)


class ToolAccessRequestCreate(BaseModel):
    tenant_id: str
    agent_id: str
    note: Optional[str] = None


class ToolAccessRequestReview(BaseModel):
    tenant_id: str
    note: Optional[str] = None


class ToolAccessRequestRead(BaseModel):
    id: str
    tenant_id: str
    tool_id: str
    tool_name: str
    tool_display_name: str
    tool_description: str
    agent_id: str
    agent_name: str
    requested_by_user_id: str
    requested_by_username: str
    status: str
    reviewed_by_username: Optional[str] = None
    review_note: Optional[str] = None
    created_at: str
    updated_at: str
    reviewed_at: Optional[str] = None


class NotificationRead(BaseModel):
    id: str
    kind: str
    title: str
    message: str
    link: Optional[str] = None
    read: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class UnreadCountRead(BaseModel):
    count: int


def _get_tool(db: Session, tenant_id: str, tool_id: str) -> Tool:
    row = db.get(Tool, tool_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Tool not found")
    return row


def _tool_owner_user_id(db: Session, tenant_id: str, tool_id: str) -> str:
    overall = get_overall_agent(db, tenant_id)
    if overall is None:
        return ""
    metadata = resource_binding_metadata(db, tenant_id, overall.id, "tool").get(tool_id, {})
    value = metadata.get("owner_user_id") or metadata.get("created_by_user_id")
    return str(value).strip() if value else ""


def _can_review(db: Session, tenant_id: str, tool_id: str, user: User) -> bool:
    if user.role == "admin":
        return True
    owner_user_id = _tool_owner_user_id(db, tenant_id, tool_id)
    return bool(owner_user_id and owner_user_id == user.id)


def _request_read(db: Session, row: ToolAccessRequest) -> ToolAccessRequestRead:
    tool = db.get(Tool, row.tool_id)
    return ToolAccessRequestRead(
        id=row.id,
        tenant_id=row.tenant_id,
        tool_id=row.tool_id,
        tool_name=row.tool_name or (tool.name if tool else ""),
        tool_display_name=(tool.display_name or tool.name) if tool else row.tool_name,
        tool_description=(tool.description or "") if tool else "",
        agent_id=row.agent_id,
        agent_name=row.agent_name,
        requested_by_user_id=row.requested_by_user_id,
        requested_by_username=row.requested_by_username,
        status=row.status,
        reviewed_by_username=row.reviewed_by_username,
        review_note=row.review_note,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
    )


def _add_notification(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    kind: str,
    title: str,
    message: str,
    link: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not user_id:
        return
    db.add(
        UserNotification(
            tenant_id=tenant_id,
            user_id=user_id,
            kind=kind,
            title=title,
            message=message,
            link=link,
            metadata_json=dict(metadata or {}),
        )
    )


@router.post("/tools/{tool_id}", response_model=ToolAccessRequestRead)
def create_tool_access_request(
    tool_id: str,
    request: ToolAccessRequestCreate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolAccessRequestRead:
    ensure_current_user_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    note = (request.note or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="请填写申请备注，说明为什么需要使用该工具")
    tool = _get_tool(db, request.tenant_id, tool_id)
    if not is_open_gallery_resource(db, request.tenant_id, "tool", tool):
        raise HTTPException(status_code=404, detail="Tool is not available in the gallery")
    agent = get_agent(db, request.tenant_id, request.agent_id)
    if agent is None or agent.is_overall:
        raise HTTPException(status_code=400, detail="请选择要使用该工具的数字员工")
    ensure_agent_scope_manager(db, request.tenant_id, agent.id, current_user)
    existing_binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == request.tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "tool",
            AgentResourceBinding.resource_id == tool.id,
            AgentResourceBinding.status != "deleted",
        )
    ).first()
    if existing_binding is not None:
        raise HTTPException(status_code=409, detail="该员工已经可以访问此工具")
    pending = db.exec(
        select(ToolAccessRequest).where(
            ToolAccessRequest.tenant_id == request.tenant_id,
            ToolAccessRequest.tool_id == tool.id,
            ToolAccessRequest.agent_id == agent.id,
            ToolAccessRequest.status == "pending",
        )
    ).first()
    if pending is not None:
        raise HTTPException(status_code=409, detail="该员工已提交过申请，请等待审批")

    row = ToolAccessRequest(
        tenant_id=request.tenant_id,
        tool_id=tool.id,
        tool_name=tool.name,
        agent_id=agent.id,
        agent_name=agent.name,
        requested_by_user_id=current_user.id,
        requested_by_username=current_user.username,
        review_note=note,
    )
    db.add(row)
    db.flush()
    owner_user_id = _tool_owner_user_id(db, request.tenant_id, tool.id)
    if owner_user_id and owner_user_id != current_user.id:
        _add_notification(
            db,
            tenant_id=request.tenant_id,
            user_id=owner_user_id,
            kind="tool_access_requested",
            title="收到新的工具使用申请",
            message=f"{current_user.username} 申请为「{agent.name}」使用工具「{tool.display_name or tool.name}」。",
            link="/enterprise/agents#tools",
            metadata={"request_id": row.id, "tool_id": tool.id},
        )
    db.commit()
    db.refresh(row)
    return _request_read(db, row)


@router.get("", response_model=list[ToolAccessRequestRead])
def list_tool_access_requests(
    tenant_id: str = Query(...),
    status: str | None = Query(default=None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ToolAccessRequestRead]:
    ensure_current_user_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    stmt = select(ToolAccessRequest).where(ToolAccessRequest.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(ToolAccessRequest.status == status)
    rows = list(db.exec(stmt.order_by(ToolAccessRequest.created_at.desc())).all())
    if current_user.role != "admin":
        overall = get_overall_agent(db, tenant_id)
        owned_tool_ids = {
            tool_id
            for tool_id, metadata in resource_binding_metadata(
                db,
                tenant_id,
                overall.id if overall else None,
                "tool",
            ).items()
            if str(metadata.get("owner_user_id") or metadata.get("created_by_user_id") or "")
            == current_user.id
        }
        rows = [row for row in rows if row.tool_id in owned_tool_ids]
    rows.sort(key=lambda row: row.created_at, reverse=True)
    rows.sort(key=lambda row: row.status != "pending")
    return [_request_read(db, row) for row in rows]


@router.post("/{request_id}/approve", response_model=ToolAccessRequestRead)
def approve_tool_access_request(
    request_id: str,
    request: ToolAccessRequestReview,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolAccessRequestRead:
    return _review_request(request_id, request, db, current_user, approve=True)


@router.post("/{request_id}/reject", response_model=ToolAccessRequestRead)
def reject_tool_access_request(
    request_id: str,
    request: ToolAccessRequestReview,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolAccessRequestRead:
    return _review_request(request_id, request, db, current_user, approve=False)


def _review_request(
    request_id: str,
    payload: ToolAccessRequestReview,
    db: Session,
    current_user: User,
    *,
    approve: bool,
) -> ToolAccessRequestRead:
    ensure_current_user_tenant(payload.tenant_id, current_user)
    ensure_tenant(db, payload.tenant_id)
    row = db.get(ToolAccessRequest, request_id)
    if row is None or row.tenant_id != payload.tenant_id:
        raise HTTPException(status_code=404, detail="申请不存在")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="该申请已处理")
    if not _can_review(db, payload.tenant_id, row.tool_id, current_user):
        raise HTTPException(status_code=403, detail="无权审批该申请")
    tool = _get_tool(db, payload.tenant_id, row.tool_id)
    agent = get_agent(db, payload.tenant_id, row.agent_id)
    if agent is None or agent.is_overall:
        raise HTTPException(status_code=404, detail="申请使用的数字员工不存在")

    row.status = "approved" if approve else "rejected"
    row.reviewed_by_user_id = current_user.id
    row.reviewed_by_username = current_user.username
    if payload.note is not None:
        row.review_note = payload.note.strip() or row.review_note
    row.reviewed_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)

    if approve:
        existing = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == payload.tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == tool.id,
            )
        ).first()
        if existing is None or existing.status == "deleted":
            ensure_private_resource_binding(
                db,
                payload.tenant_id,
                agent.id,
                "tool",
                tool.id,
                "inactive",
                metadata_json={
                    "requested_by_user_id": row.requested_by_user_id,
                    "approved_from_request_id": row.id,
                    "tool_access_granted": True,
                },
                revive=True,
            )

    _add_notification(
        db,
        tenant_id=payload.tenant_id,
        user_id=row.requested_by_user_id,
        kind="tool_access_approved" if approve else "tool_access_rejected",
        title="工具使用申请已通过" if approve else "工具使用申请被拒绝",
        message=(
            f"「{agent.name}」已获得工具「{tool.display_name or tool.name}」的使用权限，请到员工工具中启用。"
            if approve
            else f"「{agent.name}」使用工具「{tool.display_name or tool.name}」的申请被拒绝。"
        ),
        link="/enterprise/tools" if approve else "/enterprise/platform/tools",
        metadata={
            "request_id": row.id,
            "tool_id": tool.id,
            "agent_id": agent.id,
            "status": row.status,
        },
    )
    db.commit()
    db.refresh(row)
    return _request_read(db, row)


@notifications_router.get("", response_model=list[NotificationRead])
def list_notifications(
    tenant_id: str = Query(...),
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[NotificationRead]:
    ensure_current_user_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    stmt = select(UserNotification).where(
        UserNotification.tenant_id == tenant_id,
        UserNotification.user_id == current_user.id,
    )
    if unread_only:
        stmt = stmt.where(UserNotification.read_at.is_(None))
    rows = db.exec(stmt.order_by(UserNotification.created_at.desc()).limit(limit)).all()
    return [
        NotificationRead(
            id=row.id,
            kind=row.kind,
            title=row.title,
            message=row.message,
            link=row.link,
            read=row.read_at is not None,
            metadata=dict(row.metadata_json or {}),
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    ]


@notifications_router.get("/unread-count", response_model=UnreadCountRead)
def unread_notification_count(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> UnreadCountRead:
    ensure_current_user_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(UserNotification.id).where(
            UserNotification.tenant_id == tenant_id,
            UserNotification.user_id == current_user.id,
            UserNotification.read_at.is_(None),
        )
    ).all()
    return UnreadCountRead(count=len(rows))


@notifications_router.post("/{notification_id}/read")
def mark_notification_read(
    notification_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    ensure_current_user_tenant(tenant_id, current_user)
    row = db.get(UserNotification, notification_id)
    if row is None or row.tenant_id != tenant_id or row.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="消息不存在")
    if row.read_at is None:
        row.read_at = utc_now()
        db.add(row)
        db.commit()
    return {"status": "read"}


@notifications_router.post("/read-all")
def mark_all_notifications_read(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    ensure_current_user_tenant(tenant_id, current_user)
    rows = db.exec(
        select(UserNotification).where(
            UserNotification.tenant_id == tenant_id,
            UserNotification.user_id == current_user.id,
            UserNotification.read_at.is_(None),
        )
    ).all()
    now = utc_now()
    for row in rows:
        row.read_at = now
        db.add(row)
    db.commit()
    return {"status": "read"}
