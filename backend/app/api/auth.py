from __future__ import annotations

import base64
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import User, UserAvatar, utc_now
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
    # 仅 /me 与 /login 带出(头像为大字段,用户列表等批量端点不携带)
    avatar_url: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AvatarRead(BaseModel):
    avatar_url: str


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

    return LoginResponse(
        token=create_access_token(user),
        user=_user_read(user, _avatar_url_for(db, user.id)),
    )


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_session)) -> UserRead:
    return _user_read(user, _avatar_url_for(db, user.id))


MAX_AVATAR_BYTES = 2 * 1024 * 1024
# 头像类型嗅探:以实际字节头为准(防伪装 content-type),仅 png/jpeg/webp/gif
_AVATAR_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _sniff_avatar_content_type(data: bytes) -> Optional[str]:
    """按字节头识别图片类型,返回规范 content-type;非支持图片返回 None。"""
    for magic, content_type in _AVATAR_MAGIC:
        if data.startswith(magic):
            return content_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.put("/me/avatar", response_model=AvatarRead)
async def update_my_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AvatarRead:
    """上传/覆盖当前用户头像:multipart 单文件,图片 ≤2MB,以 data_url 存库(upsert)。"""
    data = await file.read()
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="头像文件超过 2MB 大小限制")
    content_type = _sniff_avatar_content_type(data)
    if not content_type:
        raise HTTPException(status_code=400, detail="仅支持 png/jpeg/webp/gif 格式的图片")
    data_url = f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"
    avatar = db.get(UserAvatar, current_user.id)
    if avatar:
        avatar.data_url = data_url
        avatar.updated_at = utc_now()
    else:
        avatar = UserAvatar(user_id=current_user.id, data_url=data_url)
    db.add(avatar)
    db.commit()
    return AvatarRead(avatar_url=data_url)


@router.delete("/me/avatar", status_code=204)
def delete_my_avatar(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> Response:
    """删除当前用户头像(无头像时幂等 204)。"""
    avatar = db.get(UserAvatar, current_user.id)
    if avatar:
        db.delete(avatar)
        db.commit()
    return Response(status_code=204)


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


def _user_read(user: User, avatar_url: Optional[str] = None) -> UserRead:
    return UserRead(
        id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        source=user.source,
        avatar_url=avatar_url,
        created_at=user.created_at.isoformat() if user.created_at else None,
        updated_at=user.updated_at.isoformat() if user.updated_at else None,
    )


def _avatar_url_for(db: Session, user_id: str) -> Optional[str]:
    avatar = db.get(UserAvatar, user_id)
    return avatar.data_url if avatar else None


def _require_admin(user: User, tenant_id: str) -> None:
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Only administrator can manage accounts")
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Cannot manage accounts for another tenant")
