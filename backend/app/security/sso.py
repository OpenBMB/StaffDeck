"""SSO 集成模块：验证 sso_oidc_sid cookie + 从 meta.platform_users 同步用户信息。

参考 data-platform/agents/api_server/security.py 的实现：
1. 从 FastAPI Request 提取 sso_oidc_sid cookie
2. 调用 SSO Backend /sso/verify 验证 cookie，返回 {username, display_name, role}
3. 直连 marmot DB 查 meta.platform_users 拿 email/department/position
4. UPSERT staffdeck users 表（首次登录自动创建，auth_source='sso'）
5. 返回 staffdeck User 对象 + 签发本地 token

角色映射：
- meta.platform_users.role (admin/editor/viewer) → staffdeck.users.role (admin/member)
- admin → admin, 其他 → member
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

import psycopg2
import psycopg2.extras
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import User, utc_now

logger = logging.getLogger(__name__)

# 角色映射：meta.role → staffdeck.role
_ROLE_MAP = {
    "admin": "admin",
    "editor": "member",
    "viewer": "member",
}

# marmot DB 连接配置（与 data-platform 一致）
_MARMOT_DSN = "host=127.0.0.1 port=5432 dbname=marmot user=postgres password=postgres"


def verify_sso_cookie(cookie: str) -> dict:
    """调 SSO Backend /sso/verify 验证 sso_oidc_sid cookie。

    返回 {username, display_name, role}；失败抛 Exception。
    """
    settings = get_settings()
    url = settings.sso_backend_url.rstrip("/") + "/sso/verify"
    req = urllib.request.Request(url)
    req.add_header("Cookie", cookie)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError(f"SSO session invalid or expired (HTTP {e.code})")
        raise ValueError(f"SSO verify API HTTP {e.code}")
    except urllib.error.URLError as e:
        raise ValueError(f"SSO verify API unreachable: {e.reason}")

    try:
        data = json.loads(payload)
    except Exception:
        raise ValueError("SSO verify API returned non-JSON")

    if "username" not in data:
        raise ValueError(f"SSO verify failed: {data.get('error', 'unknown')}")

    return data


def fetch_user_from_meta(employee_code: str) -> Optional[dict]:
    """直连 marmot DB 查 meta.platform_users 拿用户完整信息。

    返回 {employee_code, email, display_name, role, department_name, position_name} 或 None。
    """
    sql = """
        SELECT employee_code, email, display_name, role, department_name, position_name
        FROM meta.platform_users
        WHERE employee_code = %s
        LIMIT 1
    """
    try:
        with psycopg2.connect(_MARMOT_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (employee_code,))
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as e:
        logger.error(f"查询 meta.platform_users 失败 (employee_code={employee_code}): {e}")
        return None


def upsert_sso_user(sso_data: dict, db: Session) -> User:
    """根据 SSO 验证结果 UPSERT staffdeck users 表。

    - 已存在（按 username=employee_code 匹配）：更新 display_name/email/department/position/synced_at
    - 不存在：自动创建（tenant=default, role=member, auth_source='sso', password_hash=空）

    返回 staffdeck User 对象。
    """
    settings = get_settings()
    employee_code = sso_data["username"]
    sso_display_name = sso_data.get("display_name") or employee_code
    sso_role = sso_data.get("role", "viewer")

    # 从 meta 查完整信息
    meta_user = fetch_user_from_meta(employee_code)
    if meta_user:
        email = meta_user.get("email")
        display_name = meta_user.get("display_name") or sso_display_name
        department = meta_user.get("department_name")
        position = meta_user.get("position_name")
        # meta 的 role 优先于 SSO verify 返回的 role
        mapped_role = _ROLE_MAP.get(meta_user.get("role", ""), "member")
    else:
        email = None
        display_name = sso_display_name
        department = None
        position = None
        mapped_role = _ROLE_MAP.get(sso_role, "member")

    # 查 staffdeck users 表
    user = db.exec(
        select(User).where(
            User.tenant_id == settings.sso_default_tenant,
            User.username == employee_code,
        )
    ).first()

    if user:
        # 已存在：更新信息
        user.display_name = display_name
        user.email = email
        user.department = department
        user.position = position
        user.employee_code = employee_code
        user.auth_source = "sso"
        user.synced_at = utc_now()
        user.updated_at = utc_now()
        # 不自动覆盖 role（admin 可能手工调整过）
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"SSO 用户已更新: {employee_code} ({display_name})")
    else:
        # 不存在：自动创建
        user = User(
            tenant_id=settings.sso_default_tenant,
            username=employee_code,
            display_name=display_name,
            role=mapped_role,
            password_hash="",  # SSO 用户无本地密码
            employee_code=employee_code,
            email=email,
            department=department,
            position=position,
            auth_source="sso",
            synced_at=utc_now(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"SSO 用户已自动创建: {employee_code} ({display_name}), role={mapped_role}")

    return user


def sso_login_and_issue_token(cookie: str, db: Session) -> tuple[str, User]:
    """完整 SSO 登录流程：验证 cookie → UPSERT 用户 → 签发 token。

    返回 (token, user)。
    """
    # 延迟 import 避免循环依赖（auth.py ↔ sso.py）
    from app.security.auth import create_access_token

    sso_data = verify_sso_cookie(cookie)
    user = upsert_sso_user(sso_data, db)
    token = create_access_token(user)
    return token, user
