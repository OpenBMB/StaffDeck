from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import User, utc_now
from app.security.auth import create_access_token, get_current_user, hash_password, verify_password
from app.security.permissions import MEMBER_ROLE, is_admin_user
from app.security.tenant import ensure_tenant


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    tenant_id: str
    username: str
    password: str


class UserCreateRequest(BaseModel):
    tenant_id: str
    username: str
    password: str
    display_name: Optional[str] = None
    role: Literal["admin", "member"] = MEMBER_ROLE


class UserUpdateRequest(BaseModel):
    tenant_id: str
    display_name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[Literal["admin", "member"]] = None


class UserRead(BaseModel):
    id: str
    tenant_id: str
    username: str
    display_name: Optional[str] = None
    role: Literal["admin", "member"]
    source: str = "web"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class LoginResponse(BaseModel):
    token: str
    user: UserRead


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, db: Session = Depends(get_session)) -> LoginResponse:
    ensure_tenant(db, request.tenant_id)
    username = request.username.strip()
    if not username or not request.password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    user = db.exec(
        select(User).where(User.tenant_id == request.tenant_id, User.username == username)
    ).first()
    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    return LoginResponse(token=create_access_token(user), user=_user_read(user))


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user)) -> UserRead:
    return _user_read(user)


@router.post("/users", response_model=UserRead)
def create_user(
    request: UserCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only administrator can create accounts")
    if request.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot create accounts for another tenant")
    username = request.username.strip()
    if not username or not request.password:
        raise HTTPException(status_code=400, detail="Username and password are required")
    existing = db.exec(
        select(User).where(User.tenant_id == request.tenant_id, User.username == username)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Account already exists")
    user = User(
        tenant_id=request.tenant_id,
        username=username,
        display_name=(request.display_name or username).strip()[:80],
        role=request.role,
        password_hash=hash_password(request.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_read(user)


@router.get("/users", response_model=list[UserRead])
def list_users(
    tenant_id: str = Query(...),
    include_channel: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[UserRead]:
    _require_admin(current_user, tenant_id)
    statement = select(User).where(User.tenant_id == tenant_id)
    if not include_channel:
        # 渠道懒建账号(source != 'web')默认从用户管理列表隐藏
        statement = statement.where(User.source == "web")
    rows = db.exec(statement.order_by(User.created_at.desc())).all()
    return [_user_read(row) for row in rows]


@router.put("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: str,
    request: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    _require_admin(current_user, request.tenant_id)
    user = db.get(User, user_id)
    if not user or user.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="Account not found")
    if request.display_name is not None:
        display_name = request.display_name.strip()[:80]
        user.display_name = display_name or user.username
    if request.password is not None:
        password = request.password.strip()
        if password:
            user.password_hash = hash_password(password)
    if request.role is not None and request.role != user.role:
        if user.id == current_user.id:
            raise HTTPException(status_code=400, detail="Cannot change your own account role")
        user.role = request.role
    user.updated_at = utc_now()
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_read(user)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, bool]:
    _require_admin(current_user, tenant_id)
    user = db.get(User, user_id)
    if not user or user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Account not found")
    if user.id == current_user.id or is_admin_user(user):
        raise HTTPException(status_code=400, detail="Administrator account cannot be deleted")
    db.delete(user)
    db.commit()
    return {"ok": True}


def _user_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        source=user.source,
        created_at=user.created_at.isoformat() if user.created_at else None,
        updated_at=user.updated_at.isoformat() if user.updated_at else None,
    )


def _require_admin(user: User, tenant_id: str) -> None:
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Only administrator can manage accounts")
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Cannot manage accounts for another tenant")
