from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import APIClient, APICredential, User, UserAvatar, utc_now
from app.public_api.auth import generate_api_key
from app.public_api.credential_profiles import USER_FULL_ACCESS_SCOPES
from app.security.auth import (
    create_access_token,
    ensure_current_user_tenant,
    get_current_user,
    hash_password,
    verify_password,
)
from app.security.permissions import MEMBER_ROLE, is_admin_user
from app.security.tenant import ensure_tenant

logger = logging.getLogger(__name__)

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


class UserChannelIdentity(BaseModel):
    channel: str
    display_name: Optional[str] = None
    external_user_id: Optional[str] = None
    external_account_scope: Optional[str] = None


class UserRead(BaseModel):
    id: str
    tenant_id: str
    username: str
    display_name: Optional[str] = None
    role: Literal["admin", "member"]
    source: str = "web"
    disabled: bool = False
    # 仅 /me 与 /login 带出:头像资源指针(存在性标识),不内联二进制——
    # 完整 data_url 可达 2.67MB,内联会把登录/会话刷新响应与前端 localStorage 撑爆
    avatar_url: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # 渠道身份信息(仅 include_channel=True 时返回)
    channel_identities: Optional[list[UserChannelIdentity]] = None


class AvatarRead(BaseModel):
    avatar_url: str


class LoginResponse(BaseModel):
    token: str
    user: UserRead
    refresh_mode: Literal['cookie', 'none'] = 'none'


class PasswordChangeChallenge(BaseModel):
    password_change_required: Literal[True] = True
    password_change_token: str
    token_type: Literal['password_change'] = 'password_change'
    user_id: str


class PasswordChangeConfirmRequest(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=12)


class AccountAPICredentialCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_at: datetime | None = None


class AccountAPICredentialRead(BaseModel):
    id: str
    user_id: str
    name: str
    access: Literal["user_full_access"] = "user_full_access"
    key_prefix: str
    scopes: list[str] = Field(default_factory=list)
    status: str
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    revoked_at: datetime | None = None


class AccountAPICredentialCreated(AccountAPICredentialRead):
    api_key: str


ACCOUNT_API_CLIENT_PREFIX = "StaffDeck 账号全量 API"


def _require_local_account_management():
    from staffdeck_harness.runtime.control_auth import provider
    if provider() is not None:
        raise HTTPException(409, {'code': 'EXTERNAL_IDENTITY_MANAGED',
                                 'message': '企业账号由统一权限中心管理，请在权限中心修改'})


@router.post("/login", response_model=LoginResponse | PasswordChangeChallenge)
def login(request: LoginRequest, response: Response = None, http_request: Request = None, db: Session = Depends(get_session)) -> LoginResponse:
    from staffdeck_harness.runtime.control_auth import provider, project_subject
    control = provider()
    if control is not None:
        from staffdeck_harness.runtime.control_auth import ControlPasswordChange
        begin = getattr(control, 'begin_login', None)
        if http_request is not None and http_request.headers.get('x-staffdeck-auth-contract') == 'session-v1' and callable(begin):
            result = begin(request.tenant_id, request.username, request.password)
        else:
            result = control.login(request.tenant_id, request.username, request.password)
        if isinstance(result, ControlPasswordChange):
            return PasswordChangeChallenge(password_change_token=result.token, user_id=result.user_id)
        user = project_subject(db, result.subject)
        if response is not None and http_request is not None:
            _set_control_refresh(response, http_request, result.refresh_token)
        return LoginResponse(token=result.token, user=_user_read(user), refresh_mode='cookie' if result.refresh_token else 'none')
    ensure_tenant(db, request.tenant_id)
    username = request.username.strip()
    if not username or not request.password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    user = db.exec(
        select(User).where(User.tenant_id == request.tenant_id, User.username == username)
    ).first()
    if not user:
        display_name_matches = db.exec(
            select(User)
            .where(User.tenant_id == request.tenant_id, User.display_name == username)
            .limit(2)
        ).all()
        if len(display_name_matches) == 1:
            user = display_name_matches[0]
    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    return LoginResponse(
        token=create_access_token(user),
        user=_user_read(user, _avatar_pointer_for(db, user.id)),
    )


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_session)) -> UserRead:
    return _user_read(user, _avatar_pointer_for(db, user.id))


CONTROL_REFRESH_COOKIE = "staffdeck_control_refresh"


def _set_control_refresh(response, request, token):
    path = str(request.scope.get("root_path") or "").rstrip("/") + "/api/auth"
    if token:
        response.set_cookie(CONTROL_REFRESH_COOKIE, token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path=path)
    else:
        response.delete_cookie(CONTROL_REFRESH_COOKIE, path=path)


def _control_origin(request):
    from urllib.parse import urlsplit
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-origin control authentication request denied")


@router.post("/refresh", response_model=LoginResponse)
def refresh_control_session(request: Request, response: Response, db: Session = Depends(get_session)):
    from staffdeck_harness.runtime.control_auth import provider, project_subject
    _control_origin(request)
    control = provider()
    token = request.cookies.get(CONTROL_REFRESH_COOKIE)
    if not token or control is None or not callable(getattr(control, "refresh", None)):
        raise HTTPException(401, "Control session expired")
    result = control.refresh(token)
    user = project_subject(db, result.subject)
    _set_control_refresh(response, request, result.refresh_token)
    return LoginResponse(token=result.token, user=_user_read(user), refresh_mode='cookie')


@router.post('/temporary-password/confirm')
def confirm_temporary_password(body: PasswordChangeConfirmRequest, request: Request):
    from staffdeck_harness.runtime.control_auth import provider
    _control_origin(request)
    confirm = getattr(provider(), 'confirm_temporary_password', None)
    if not callable(confirm):
        raise HTTPException(501, {'code': 'FEATURE_UNAVAILABLE', 'message': '当前身份服务不支持首次改密'})
    confirm(body.token, body.new_password)
    return {'confirmed': True}


@router.post("/logout")
def logout_control_session(request: Request, response: Response):
    from staffdeck_harness.runtime.control_auth import provider
    _control_origin(request)
    control = provider()
    token = request.cookies.get(CONTROL_REFRESH_COOKIE)
    if token and control is not None and callable(getattr(control, "logout", None)):
        control.logout(token)
    _set_control_refresh(response, request, None)
    return {"logged_out": True}


MAX_AVATAR_BYTES = 2 * 1024 * 1024
# multipart 边界与头部开销的上限估计:Content-Length 预检放行正常图片,拦截明显超限请求
_AVATAR_MULTIPART_OVERHEAD = 64 * 1024
# 头像资源路径:login/me 返回的 avatar_url 即此指针,前端凭它用认证请求拉取字节
AVATAR_RESOURCE_PATH = "/api/auth/me/avatar"
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


@router.get("/me/avatar")
def get_my_avatar(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> Response:
    """头像资源端点:返回图片字节(不内联进 login/me,避免大字段进会话存储)。"""
    avatar = db.get(UserAvatar, current_user.id)
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    parsed = _parse_avatar_data_url(avatar.data_url)
    if not parsed:
        logger.warning("用户 %s 的头像数据损坏,按不存在处理", current_user.id)
        raise HTTPException(status_code=404, detail="Avatar not found")
    data, content_type = parsed
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, no-cache"},
    )


@router.put("/me/avatar", response_model=AvatarRead)
async def update_my_avatar(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AvatarRead:
    """上传/覆盖当前用户头像:multipart 单文件,图片 ≤2MB,以 data_url 存库(upsert)。"""
    # 先按 Content-Length 快速拒绝明显超限的请求,避免把超大请求体完整读入内存
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit():
        if int(content_length) > MAX_AVATAR_BYTES + _AVATAR_MULTIPART_OVERHEAD:
            raise HTTPException(status_code=413, detail="头像文件超过 2MB 大小限制")
    # 限量读取(最多 MAX+1 字节)做硬性兜底,覆盖 Content-Length 缺失或虚报的情况
    data = await file.read(MAX_AVATAR_BYTES + 1)
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
    # 响应同样不内联二进制:返回资源指针,前端经 GET /me/avatar 拉取字节
    return AvatarRead(avatar_url=AVATAR_RESOURCE_PATH)


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
    _require_local_account_management()
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
    request: Request = None,
) -> list[UserRead]:
    ensure_current_user_tenant(tenant_id, current_user)
    from staffdeck_harness.runtime.control_auth import provider
    control = provider()
    if control is not None:
        reader = getattr(control, 'list_users', None)
        if not callable(reader):
            raise HTTPException(503, '当前身份来源未提供用户目录')
        if not is_admin_user(current_user):
            return [_user_read(current_user)]
        authorization = request.headers.get('authorization', '') if request else ''
        if not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Not authenticated')
        rows = [UserRead.model_validate(row) for row in reader(db.info['staffdeck_control_subject'], authorization[7:])]
    else:
        statement = select(User).where(User.tenant_id == tenant_id)
        # Internal directory excludes guest and group identities for ordinary users.
        if not is_admin_user(current_user) or not include_channel:
            statement = statement.where(User.source == "web")
        rows = [_user_read(row) for row in db.exec(statement.order_by(User.created_at.desc())).all()]
    if include_channel:
        # 附带内部成员已绑定的渠道身份，供处理人选择器判断当前 binding 是否可达。
        from app.db.models import ChannelIdentity

        all_identities: dict[str, list[ChannelIdentity]] = {}
        user_ids = [row.id for row in rows]
        identities = (
            db.exec(
                select(ChannelIdentity).where(
                    ChannelIdentity.tenant_id == tenant_id,
                    ChannelIdentity.staffdeck_user_id.in_(user_ids),
                    ~ChannelIdentity.external_user_id.startswith("group:"),
                )
            ).all()
            if user_ids
            else []
        )
        for ci in identities:
            all_identities.setdefault(ci.staffdeck_user_id, []).append(ci)
        result = []
        for row in rows:
            cis = all_identities.get(row.id)
            identities = (
                [
                    UserChannelIdentity(
                        channel=ci.channel,
                        display_name=ci.display_name,
                        external_user_id=ci.external_user_id,
                        external_account_scope=ci.external_account_scope,
                    )
                    for ci in cis
                ]
                if cis
                else None
            )
            result.append(row.model_copy(update={'channel_identities': identities}))
        return result
    return rows


@router.get(
    "/me/api-credentials",
    response_model=list[AccountAPICredentialRead],
)
def list_account_api_credentials(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AccountAPICredentialRead]:
    client = _ensure_account_api_client(db, current_user.tenant_id, current_user)
    if not client:
        return []
    rows = db.exec(
        select(APICredential)
        .where(
            APICredential.tenant_id == current_user.tenant_id,
            APICredential.client_id == client.id,
            APICredential.agent_id.is_(None),
        )
        .order_by(APICredential.created_at.desc())
    ).all()
    db.commit()
    return [_account_api_credential_read(row, current_user.id) for row in rows]


@router.post(
    "/me/api-credentials",
    response_model=AccountAPICredentialCreated,
)
def create_account_api_credential(
    request: AccountAPICredentialCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AccountAPICredentialCreated:
    client = _ensure_account_api_client(db, current_user.tenant_id, current_user)
    token, prefix, digest = generate_api_key()
    row = APICredential(
        tenant_id=current_user.tenant_id,
        client_id=client.id,
        agent_id=None,
        name=request.name.strip(),
        key_prefix=prefix,
        key_digest=digest,
        scopes_json=sorted(USER_FULL_ACCESS_SCOPES),
        expires_at=request.expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return AccountAPICredentialCreated(
        **_account_api_credential_read(row, current_user.id).model_dump(),
        api_key=token,
    )


@router.post(
    "/me/api-credentials/{credential_id}/rotate",
    response_model=AccountAPICredentialCreated,
)
def rotate_account_api_credential(
    credential_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AccountAPICredentialCreated:
    row = _get_account_api_credential(
        db, current_user.tenant_id, current_user.id, credential_id
    )
    token, prefix, digest = generate_api_key()
    row.key_prefix = prefix
    row.key_digest = digest
    row.status = "active"
    row.revoked_at = None
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return AccountAPICredentialCreated(
        **_account_api_credential_read(row, current_user.id).model_dump(),
        api_key=token,
    )


@router.post(
    "/me/api-credentials/{credential_id}/revoke",
    response_model=AccountAPICredentialRead,
)
def revoke_account_api_credential(
    credential_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AccountAPICredentialRead:
    row = _get_account_api_credential(
        db, current_user.tenant_id, current_user.id, credential_id
    )
    row.status = "revoked"
    row.revoked_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _account_api_credential_read(row, current_user.id)


@router.put("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: str,
    request: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    _require_admin(current_user, request.tenant_id)
    _require_local_account_management()
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
    _require_local_account_management()
    user = db.get(User, user_id)
    if not user or user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Account not found")
    if user.id == current_user.id or is_admin_user(user):
        raise HTTPException(status_code=400, detail="Administrator account cannot be deleted")
    # 头像为独立小表、无外键级联:显式随用户删除,避免残留孤儿记录
    avatar = db.get(UserAvatar, user_id)
    if avatar:
        db.delete(avatar)
    db.delete(user)
    db.commit()
    return {"ok": True}


def _user_read(
    user: User,
    avatar_url: Optional[str] = None,
    channel_identities: Optional[list[UserChannelIdentity]] = None,
) -> UserRead:
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
        channel_identities=channel_identities,
    )


def _avatar_pointer_for(db: Session, user_id: str) -> Optional[str]:
    """头像存在性指针:有头像返回资源路径,无返回 None(绝不内联二进制)。"""
    avatar = db.get(UserAvatar, user_id)
    return AVATAR_RESOURCE_PATH if avatar else None


def _parse_avatar_data_url(data_url: str) -> Optional[tuple[bytes, str]]:
    """拆解 data:image/*;base64,... 为(字节, content-type);非法返回 None。"""
    try:
        meta, payload = data_url.split(",", 1)
        content_type = meta.removeprefix("data:").removesuffix(";base64")
        if not content_type.startswith("image/"):
            return None
        return base64.b64decode(payload), content_type
    except (ValueError, TypeError):
        return None


def _require_admin(user: User, tenant_id: str) -> None:
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Only administrator can manage accounts")
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Cannot manage accounts for another tenant")


def _account_api_client_name(user_id: str) -> str:
    return f"{ACCOUNT_API_CLIENT_PREFIX}:{user_id}"


def _find_account_api_client(
    db: Session,
    tenant_id: str,
    user_id: str,
) -> APIClient | None:
    from app.public_api.runtime import realm_client_name
    return db.exec(
        select(APIClient).where(
            APIClient.tenant_id == tenant_id,
            APIClient.name == realm_client_name(db, _account_api_client_name(user_id)),
        )
    ).first()


def _ensure_account_api_client(db: Session, tenant_id: str, user: User) -> APIClient:
    from app.public_api.runtime import realm_client_name, stamp_client
    row = _find_account_api_client(db, tenant_id, user.id)
    required_scopes = sorted(USER_FULL_ACCESS_SCOPES)
    if row:
        row.created_by_user_id = user.id
        row.status = "active"
        row.scopes_json = required_scopes
        row.metadata_json = {
            **dict(row.metadata_json or {}),
            "managed_by": "account_settings",
            "credential_mode": "user_full_access",
            "subject_user_id": user.id,
        }
        row.updated_at = utc_now()
        db.add(row)
        credentials = db.exec(
            select(APICredential).where(
                APICredential.tenant_id == tenant_id,
                APICredential.client_id == row.id,
                APICredential.agent_id.is_(None),
            )
        ).all()
        for credential in credentials:
            credential.scopes_json = required_scopes
            credential.updated_at = utc_now()
            db.add(credential)
        db.flush()
        return row
    row = APIClient(
        tenant_id=tenant_id,
        name=realm_client_name(db, _account_api_client_name(user.id)),
        description=f"账号 {user.username} 的全量 API 密钥，权限随账号可见员工动态变化。",
        scopes_json=required_scopes,
        status="active",
        created_by_user_id=user.id,
        metadata_json={
            "managed_by": "account_settings",
            "credential_mode": "user_full_access",
            "subject_user_id": user.id,
        },
    )
    stamp_client(db, row)
    db.add(row)
    db.flush()
    return row


def _get_account_api_credential(
    db: Session,
    tenant_id: str,
    user_id: str,
    credential_id: str,
) -> APICredential:
    client = _find_account_api_client(db, tenant_id, user_id)
    row = db.get(APICredential, credential_id)
    if (
        not client
        or not row
        or row.tenant_id != tenant_id
        or row.client_id != client.id
        or row.agent_id is not None
    ):
        raise HTTPException(status_code=404, detail="Account API credential not found")
    return row


def _account_api_credential_read(
    row: APICredential,
    user_id: str,
) -> AccountAPICredentialRead:
    return AccountAPICredentialRead(
        id=row.id,
        user_id=user_id,
        name=row.name,
        key_prefix=f"{row.key_prefix}…",
        scopes=list(row.scopes_json or []),
        status=row.status,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )
