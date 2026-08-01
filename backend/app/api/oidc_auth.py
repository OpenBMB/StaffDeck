from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session

from app.db import get_session
from app.security.auth import create_access_token
from app.security.oidc import (
    OIDCLoginError,
    OIDCNotConfigured,
    OIDCStateError,
    build_authorize_url,
    complete_login,
    oidc_display_name,
    oidc_ready,
    resolve_redirect_uri,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/oidc", tags=["auth"])

# 回调成功后携带令牌落地到登录页;令牌放在 URL fragment(# 之后),不会进入
# 服务器日志/浏览器历史外的任何记录,由前端读取后写入会话存储并清理 hash。
_LOGIN_LANDING = "/login"


class OIDCConfig(BaseModel):
    enabled: bool
    name: str


@router.get("/config", response_model=OIDCConfig)
def oidc_config() -> OIDCConfig:
    """登录页探测 SSO 可用性(公开)。"""
    return OIDCConfig(enabled=oidc_ready(), name=oidc_display_name())


@router.get("/authorize")
def oidc_authorize(request: Request, db: Session = Depends(get_session)) -> RedirectResponse:
    """发起 PKCE 授权流:生成并持久化 state,302 跳转 IdP。"""
    try:
        url = build_authorize_url(db, resolve_redirect_uri(request))
    except OIDCNotConfigured:
        return _error_redirect("OIDC 未启用或配置不完整")
    except (OIDCLoginError, OIDCStateError) as exc:
        logger.warning("OIDC authorize failed: %s", exc)
        return _error_redirect("身份提供方配置异常，请联系管理员")
    return RedirectResponse(url=url, status_code=302)


@router.get("/callback")
def oidc_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    """IdP 回调:换 token → 校验 ID token → 映射用户 → 签发 StaffDeck JWT。"""
    if not code or not state:
        return _error_redirect("回调缺少 code 或 state 参数")
    try:
        user = complete_login(
            db,
            authorization_response=str(request.url),
            state=state,
            redirect_uri=resolve_redirect_uri(request),
        )
    except OIDCNotConfigured:
        return _error_redirect("OIDC 未启用或配置不完整")
    except OIDCStateError:
        return _error_redirect("登录状态无效或已过期，请重新发起登录")
    except OIDCLoginError as exc:
        message = str(exc) if str(exc) else "登录失败，请联系管理员"
        return _error_redirect(message)
    except Exception:  # 兜底:网络/解析等未预期异常不向用户泄露细节
        logger.exception("OIDC callback unexpected failure")
        return _error_redirect("登录失败，请联系管理员")

    token = create_access_token(user)
    return RedirectResponse(url=f"{_LOGIN_LANDING}#oidc_token={token}", status_code=302)


def _error_redirect(message: str) -> RedirectResponse:
    params = urlencode({"oidc_error": message})
    return RedirectResponse(url=f"{_LOGIN_LANDING}?{params}", status_code=302)
