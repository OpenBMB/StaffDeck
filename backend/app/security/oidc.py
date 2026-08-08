from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from authlib.common.security import generate_token
from authlib.oidc.core import CodeIDToken
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet
from sqlalchemy import delete
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.db.models import OIDCAuthState, User, utc_now
from app.security.auth import hash_password

logger = logging.getLogger(__name__)

# 授权流状态有效期:用户从点击 SSO 到回调完成不应超过 10 分钟
_STATE_TTL_SECONDS = 600
# discovery 文档缓存时长(IdP 元数据极少变化,按小时缓存即可)
_DISCOVERY_CACHE_TTL_SECONDS = 3600
# 对 IdP 的 HTTP 请求超时(秒)
_IDP_TIMEOUT_SECONDS = 30


class OIDCNotConfigured(Exception):
    """OIDC 未启用或配置不完整。"""


class OIDCStateError(Exception):
    """授权 state 无效、过期或已使用。"""


class OIDCLoginError(Exception):
    """令牌校验或用户映射失败。"""


def oidc_ready(settings: Settings | None = None) -> bool:
    """OIDC 是否已启用且配置完整(issuer + client_id 为硬性前提)。"""
    settings = settings or get_settings()
    return bool(
        settings.oidc_enabled
        and settings.oidc_issuer.strip()
        and settings.oidc_client_id.strip()
    )


def oidc_display_name(settings: Settings | None = None) -> str:
    """登录页 SSO 按钮展示名:优先 OIDC_NAME 配置,回退为 issuer 主机名。"""
    settings = settings or get_settings()
    if configured := settings.oidc_name.strip():
        return configured
    issuer = settings.oidc_issuer.strip()
    host = issuer.split("://", 1)[-1].split("/", 1)[0] if issuer else ""
    return host or "SSO"


def _scope_list(settings: Settings) -> list[str]:
    scopes = [s.strip() for s in settings.oidc_scopes.split(",") if s.strip()]
    return scopes or ["openid", "profile", "email"]


_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def clear_oidc_cache() -> None:
    """清空 discovery 缓存(测试用)。"""
    _discovery_cache.clear()


def _discovery(settings: Settings) -> dict[str, Any]:
    now = time.monotonic()
    cached = _discovery_cache.get(settings.oidc_issuer)
    if cached and now - cached[0] < _DISCOVERY_CACHE_TTL_SECONDS:
        return cached[1]
    discovery_url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        resp = httpx.get(discovery_url, timeout=_IDP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        metadata = resp.json()
    except Exception as exc:
        raise OIDCLoginError("Failed to load IdP discovery document") from exc
    _discovery_cache[settings.oidc_issuer] = (now, metadata)
    return metadata


def resolve_redirect_uri(request, settings: Settings | None = None) -> str:
    """回调地址:显式配置优先(应为完整 URL),否则按请求 base_url 推导。

    推导结果固定指向本服务自身的 /api/auth/oidc/callback 路径,不接受任何
    外部输入,杜绝 open redirect。
    """
    settings = settings or get_settings()
    configured = settings.oidc_redirect_uri.strip()
    if configured:
        return configured
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/auth/oidc/callback"


def _pkce_challenge(code_verifier: str) -> str:
    """S256 code_challenge = urlsafe_base64(sha256(verifier)) 去 padding。"""
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def build_authorize_url(db: Session, redirect_uri: str, settings: Settings | None = None) -> str:
    """生成 PKCE 授权流:落库 state/verifier/nonce,返回 IdP 授权跳转 URL。"""
    settings = settings or get_settings()
    if not oidc_ready(settings):
        raise OIDCNotConfigured("OIDC is not configured")
    metadata = _discovery(settings)
    authorization_endpoint = metadata.get("authorization_endpoint")
    if not authorization_endpoint:
        raise OIDCLoginError("IdP discovery missing authorization_endpoint")

    state = generate_token()
    code_verifier = generate_token(48)
    nonce = generate_token()
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(_scope_list(settings)),
        "state": state,
        "nonce": nonce,
        "code_challenge": _pkce_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    separator = "&" if "?" in authorization_endpoint else "?"
    url = f"{authorization_endpoint}{separator}{urlencode(params)}"

    now = utc_now()
    # 惰性清理:顺带删除过期 state,避免小表无限增长
    db.exec(delete(OIDCAuthState).where(OIDCAuthState.expires_at < now))
    db.add(
        OIDCAuthState(
            state=state,
            code_verifier=code_verifier,
            nonce=nonce,
            created_at=now,
            expires_at=now + timedelta(seconds=_STATE_TTL_SECONDS),
        )
    )
    db.commit()
    return url


def complete_login(
    db: Session,
    authorization_response: str,
    state: str,
    redirect_uri: str,
    settings: Settings | None = None,
) -> User:
    """消费授权回调:校验 state → 换 token → 校验 ID token → 映射/创建用户。"""
    settings = settings or get_settings()
    if not oidc_ready(settings):
        raise OIDCNotConfigured("OIDC is not configured")

    auth_state = db.get(OIDCAuthState, state)
    if not auth_state:
        raise OIDCStateError("Invalid or expired state")
    # 一次性消费:先删后校验,天然防重放(重放时 state 已不存在)
    db.delete(auth_state)
    db.commit()
    if auth_state.expires_at < utc_now():
        raise OIDCStateError("State expired")

    metadata = _discovery(settings)
    token_endpoint = metadata.get("token_endpoint")
    if not token_endpoint:
        raise OIDCLoginError("IdP discovery missing token_endpoint")

    token = _exchange_token(
        settings, token_endpoint, authorization_response, auth_state.code_verifier, redirect_uri
    )
    claims = _validate_id_token(settings, metadata, token, auth_state.nonce)
    claims = _enrich_claims_with_userinfo(settings, metadata, token, claims)
    return _resolve_user(db, claims, settings)


def _exchange_token(
    settings: Settings,
    token_endpoint: str,
    authorization_response: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """授权码换令牌:client_secret_basic 客户端认证 + PKCE verifier。"""
    try:
        resp = httpx.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": _extract_code(authorization_response),
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            auth=(settings.oidc_client_id, settings.oidc_client_secret or ""),
            timeout=_IDP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        token = resp.json()
    except Exception as exc:
        logger.warning("OIDC token exchange failed: %s", exc)
        raise OIDCLoginError("Token exchange failed") from exc
    if not token.get("id_token"):
        raise OIDCLoginError("Token response missing id_token")
    return token


def _extract_code(authorization_response: str) -> str:
    """从完整回调 URL 提取授权码;提取失败抛错(不向用户泄露响应细节)。"""
    code = parse_qs(urlparse(authorization_response).query).get("code", [""])[0]
    if not code:
        raise OIDCLoginError("Authorization response missing code")
    return code


def _validate_id_token(
    settings: Settings,
    metadata: dict[str, Any],
    token: dict[str, Any],
    nonce: str,
) -> dict[str, Any]:
    """校验 ID token:签名(JWKS) + iss/aud/exp/iat/nonce。

    实现与 authlib 官方 async 客户端 parse_id_token 同构:
    joserfc 解码签名,CodeIDToken 校验标准 claims。
    """
    jwks_uri = metadata.get("jwks_uri")
    if not jwks_uri:
        raise OIDCLoginError("IdP discovery missing jwks_uri")
    try:
        resp = httpx.get(jwks_uri, timeout=_IDP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        key_set = KeySet.import_key_set(resp.json())
    except Exception as exc:
        logger.warning("OIDC JWKS fetch failed: %s", exc)
        raise OIDCLoginError("Failed to load IdP signing keys") from exc

    algorithms = metadata.get("id_token_signing_alg_values_supported") or ["RS256"]
    try:
        decoded = jose_jwt.decode(
            token["id_token"],
            key=key_set,
            algorithms=algorithms,
        )
    except Exception as exc:
        logger.warning("OIDC id_token signature verification failed: %s", exc)
        raise OIDCLoginError("Invalid id_token") from exc

    claims_options = {"iss": {"values": [metadata["issuer"]]}}
    claims = CodeIDToken(
        decoded.claims,
        decoded.header,
        claims_options,
        {
            "nonce": nonce,
            "client_id": settings.oidc_client_id,
            "access_token": token.get("access_token", ""),
        },
    )
    try:
        claims.validate(leeway=settings.oidc_clock_skew_seconds)
    except Exception as exc:
        logger.warning("OIDC id_token claims validation failed: %s", exc)
        raise OIDCLoginError("Invalid id_token") from exc
    return dict(claims)


def _enrich_claims_with_userinfo(
    settings: Settings,
    metadata: dict[str, Any],
    token: dict[str, Any],
    claims: dict[str, Any],
) -> dict[str, Any]:
    """ID token claims 基础上补充 userinfo(部分 IdP 不在 ID token 中带 email)。"""
    userinfo_endpoint = metadata.get("userinfo_endpoint")
    if not userinfo_endpoint or not token.get("access_token"):
        return claims
    try:
        resp = httpx.get(
            userinfo_endpoint,
            headers={"Authorization": f"Bearer {token['access_token']}"},
            timeout=_IDP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        userinfo = resp.json()
    except Exception as exc:
        # userinfo 失败不阻断登录:ID token 已通过签名与 claims 校验
        logger.warning("OIDC userinfo fetch failed (continuing with id_token): %s", exc)
        return claims
    merged = dict(claims)
    for key in ("sub", "preferred_username", "email", "name"):
        if userinfo.get(key) and not merged.get(key):
            merged[key] = userinfo[key]
    return merged


def _resolve_user(db: Session, claims: dict[str, Any], settings: Settings) -> User:
    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise OIDCLoginError("ID token missing sub claim")
    tenant_id = settings.oidc_tenant_id.strip() or "tenant_demo"

    user = db.exec(
        select(User).where(User.tenant_id == tenant_id, User.oidc_sub == sub)
    ).first()
    if user:
        return user

    if not settings.oidc_auto_provision:
        raise OIDCLoginError(
            "OIDC 账号尚未绑定 StaffDeck 账号，请联系管理员创建账号后重试"
        )

    username = _unique_username(db, tenant_id, claims, sub)
    display_name = str(claims.get("name") or claims.get("preferred_username") or username)[:80]
    # OIDC 账号不设可用密码:随机口令仅满足模型非空约束,无法用于密码登录
    user = User(
        tenant_id=tenant_id,
        username=username,
        display_name=display_name,
        role=settings.oidc_default_role,
        source="oidc",
        oidc_sub=sub,
        password_hash=hash_password(secrets.token_urlsafe(32)),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Auto-provisioned OIDC user %s (tenant=%s sub=%s)", user.id, tenant_id, sub)
    return user


def _unique_username(db: Session, tenant_id: str, claims: dict[str, Any], sub: str) -> str:
    preferred = str(
        claims.get("preferred_username")
        or (str(claims.get("email") or "").split("@", 1)[0])
        or f"oidc_{hashlib.sha256(sub.encode('utf-8')).hexdigest()[:12]}"
    )
    preferred = "".join(ch for ch in preferred if ch.isalnum() or ch in "._-")[:40] or "oidc_user"
    candidate = preferred
    suffix = 2
    while db.exec(
        select(User.id).where(User.tenant_id == tenant_id, User.username == candidate)
    ).first():
        candidate = f"{preferred[:36]}_{suffix}"
        suffix += 1
    return candidate
