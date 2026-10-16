"""登录页「记住账号」= 30 天内免登。

免登窗口由服务端签发的 token 有效期决定，所以这里锁定三件事：
1. 不勾选维持现状（14 天），老客户端不传 remember 也是这个行为；
2. 勾选后有效期变成 30 天；
3. 回给前端的 expires_at 与 token 里的 exp 一致，且过期 token 会被拒。
"""

from __future__ import annotations

import time

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.auth import LoginRequest, login
from app.db.models import Tenant, User
from app.security.auth import (
    REMEMBER_TOKEN_TTL_SECONDS,
    TOKEN_TTL_SECONDS,
    _encode_token,
    hash_password,
    read_token_payload,
)

TENANT = "tenant_demo"


def test_login_without_remember_keeps_fourteen_day_window() -> None:
    with _session() as db:
        _seed(db)
        before = int(time.time())
        response = login(LoginRequest(tenant_id=TENANT, username="admin", password="secret"), db)

    assert response.expires_at is not None
    assert abs(response.expires_at - (before + TOKEN_TTL_SECONDS)) <= 5
    # 凭证本身带同样的过期时间：前端展示的窗口与后端判定不允许出现偏差
    payload = read_token_payload(response.token)
    assert payload is not None
    assert payload["exp"] == response.expires_at


def test_remember_login_extends_window_to_thirty_days() -> None:
    with _session() as db:
        _seed(db)
        before = int(time.time())
        response = login(
            LoginRequest(
                tenant_id=TENANT, username="admin", password="secret", remember=True
            ),
            db,
        )

    assert response.expires_at is not None
    assert abs(response.expires_at - (before + REMEMBER_TOKEN_TTL_SECONDS)) <= 5
    assert REMEMBER_TOKEN_TTL_SECONDS == 60 * 60 * 24 * 30
    payload = read_token_payload(response.token)
    assert payload is not None
    assert payload["exp"] == response.expires_at


def test_expired_token_is_rejected_so_the_window_is_a_hard_ceiling() -> None:
    expired = _encode_token(
        {
            "tenant_id": TENANT,
            "user_id": "user_admin",
            "username": "admin",
            "exp": int(time.time()) - 5,
        }
    )
    assert read_token_payload(expired) is None


def _seed(db: Session) -> None:
    db.add(Tenant(id=TENANT, name="Demo"))
    db.add(
        User(
            id="user_admin",
            tenant_id=TENANT,
            username="admin",
            role="admin",
            password_hash=hash_password("secret"),
        )
    )
    db.commit()


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
