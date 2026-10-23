"""数字员工分享链接:创建、有效期、访客身份与权限边界。"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.agent_shares import (
    AgentShareCreateRequest,
    AgentShareVisitorSessionRequest,
    create_agent_share,
    open_agent_share_session,
    read_agent_share,
    resolve_share_link,
)
from app.api.chat import _apply_share_scope
from app.db.models import (
    AgentProfile,
    AgentShareLink,
    AgentShareVisitor,
    ModelConfig,
    Tenant,
    User,
    utc_now,
)
from app.main import share_scope_allows_path
from app.security.auth import (
    SHARE_TOKEN_MAX_TTL_SECONDS,
    ShareScope,
    create_share_access_token,
    read_token_payload,
    share_scope_from_payload,
)
from app.security.tenant import ensure_tenant
from app.session.session_schema import ChatTurnRequest


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed(engine) -> None:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            User(
                id="user_owner",
                tenant_id="tenant_demo",
                username="owner",
                role="member",
                password_hash="",
            )
        )
        db.add(
            User(
                id="user_other",
                tenant_id="tenant_demo",
                username="other",
                role="member",
                password_hash="",
            )
        )
        db.add(
            AgentProfile(
                id="agent_1",
                tenant_id="tenant_demo",
                name="销售助手",
                status="active",
                metadata_json={"owner_user_id": "user_owner"},
            )
        )
        db.add(
            ModelConfig(
                id="model_1",
                tenant_id="tenant_demo",
                name="DeepSeek V4",
                model="deepseek-v4",
                api_key_encrypted="x",
            )
        )
        db.commit()


def _create(engine, *, ttl: str = "2h", agent_id: str = "agent_1"):
    with Session(engine) as db:
        owner = db.get(User, "user_owner")
        return create_agent_share(
            AgentShareCreateRequest(
                tenant_id="tenant_demo",
                agent_id=agent_id,
                model_config_id="model_1",
                ttl=ttl,
            ),
            current_user=owner,
            db=db,
        )


def test_create_share_sets_expiry_for_each_ttl() -> None:
    engine = _test_engine()
    _seed(engine)

    thirty = _create(engine, ttl="30m")
    assert thirty.expires_at is not None
    assert 25 * 60 <= (thirty.expires_at - utc_now()).total_seconds() <= 30 * 60

    two_hours = _create(engine, ttl="2h")
    assert two_hours.expires_at is not None
    assert 115 * 60 <= (two_hours.expires_at - utc_now()).total_seconds() <= 120 * 60

    forever = _create(engine, ttl="forever")
    assert forever.expires_at is None


def test_create_share_rejects_agent_owned_by_someone_else() -> None:
    engine = _test_engine()
    _seed(engine)
    with Session(engine) as db:
        other = db.get(User, "user_other")
        with pytest.raises(HTTPException) as exc:
            create_agent_share(
                AgentShareCreateRequest(
                    tenant_id="tenant_demo",
                    agent_id="agent_1",
                    model_config_id="model_1",
                    ttl="2h",
                ),
                current_user=other,
                db=db,
            )
    assert exc.value.status_code == 403


def test_create_share_rejects_disabled_model() -> None:
    engine = _test_engine()
    _seed(engine)
    with Session(engine) as db:
        model = db.get(ModelConfig, "model_1")
        model.enabled = False
        db.add(model)
        db.commit()
    with Session(engine) as db:
        owner = db.get(User, "user_owner")
        with pytest.raises(HTTPException) as exc:
            create_agent_share(
                AgentShareCreateRequest(
                    tenant_id="tenant_demo",
                    agent_id="agent_1",
                    model_config_id="model_1",
                    ttl="2h",
                ),
                current_user=owner,
                db=db,
            )
    assert exc.value.status_code == 404


def test_resolve_share_link_rejects_expired_and_revoked() -> None:
    engine = _test_engine()
    _seed(engine)
    share = _create(engine)

    with Session(engine) as db:
        row = db.get(AgentShareLink, share.id)
        row.expires_at = utc_now().replace(year=utc_now().year - 1)
        db.add(row)
        db.commit()
    with Session(engine) as db:
        with pytest.raises(HTTPException) as exc:
            resolve_share_link(db, share.token)
    assert exc.value.status_code == 410

    with Session(engine) as db:
        row = db.get(AgentShareLink, share.id)
        row.expires_at = None
        row.revoked_at = utc_now()
        db.add(row)
        db.commit()
    with Session(engine) as db:
        with pytest.raises(HTTPException) as exc:
            resolve_share_link(db, share.token)
    assert exc.value.status_code == 404


def test_read_agent_share_exposes_display_fields_only() -> None:
    engine = _test_engine()
    _seed(engine)
    share = _create(engine)
    with Session(engine) as db:
        info = read_agent_share(share.token, db)
    assert info.agent_name == "销售助手"
    assert info.model_label == "DeepSeek V4"
    assert not hasattr(info, "tenant_id")


def test_open_session_creates_guest_and_reuses_it_by_visitor_key() -> None:
    engine = _test_engine()
    _seed(engine)
    share = _create(engine)

    with Session(engine) as db:
        first = open_agent_share_session(
            share.token, AgentShareVisitorSessionRequest(), db
        )
    assert first.visitor_key
    assert first.user.tenant_id == "tenant_demo"
    assert first.model_config_id == "model_1"

    with Session(engine) as db:
        guest = db.get(User, first.user.id)
        assert guest is not None
        assert guest.source == "share"
        assert guest.username not in {"owner", "other"}

    with Session(engine) as db:
        second = open_agent_share_session(
            share.token, AgentShareVisitorSessionRequest(visitor_key=first.visitor_key), db
        )
    # 同一浏览器(同一 visitor_key)复用同一个虚拟用户 -> 多轮上下文延续
    assert second.visitor_key == first.visitor_key
    assert second.user.id == first.user.id

    with Session(engine) as db:
        assert len(db.exec(select(AgentShareVisitor)).all()) == 1


def test_open_session_does_not_reuse_visitor_key_across_shares() -> None:
    engine = _test_engine()
    _seed(engine)
    first_share = _create(engine)
    second_share = _create(engine)

    with Session(engine) as db:
        first = open_agent_share_session(
            first_share.token, AgentShareVisitorSessionRequest(), db
        )
    with Session(engine) as db:
        second = open_agent_share_session(
            second_share.token, AgentShareVisitorSessionRequest(visitor_key=first.visitor_key), db
        )
    assert second.visitor_key != first.visitor_key
    assert second.user.id != first.user.id


def test_open_session_bumps_access_count() -> None:
    engine = _test_engine()
    _seed(engine)
    share = _create(engine)
    for _ in range(3):
        with Session(engine) as db:
            open_agent_share_session(share.token, AgentShareVisitorSessionRequest(), db)
    with Session(engine) as db:
        row = db.get(AgentShareLink, share.id)
        assert row.access_count == 3
        assert row.last_access_at is not None


def test_open_session_token_ttl_is_bounded_by_share_expiry() -> None:
    engine = _test_engine()
    _seed(engine)
    share = _create(engine, ttl="30m")
    with Session(engine) as db:
        session_read = open_agent_share_session(
            share.token, AgentShareVisitorSessionRequest(), db
        )
    payload = read_token_payload(session_read.access_token)
    assert payload is not None
    remaining = int(payload["exp"]) - int(time.time())
    assert 25 * 60 <= remaining <= 30 * 60
    assert remaining <= SHARE_TOKEN_MAX_TTL_SECONDS


def test_access_token_carries_share_scope() -> None:
    engine = _test_engine()
    _seed(engine)
    with Session(engine) as db:
        guest = User(
            id="guest_1",
            tenant_id="tenant_demo",
            username="share_guest_1",
            role="member",
            source="share",
            password_hash="",
        )
    token = create_share_access_token(
        guest,
        share_id="agentshare_1",
        share_token="tok",
        agent_id="agent_1",
        model_config_id="model_1",
        ttl_seconds=600,
    )
    scope = share_scope_from_payload(read_token_payload(token) or {})
    assert scope == ShareScope(
        share_id="agentshare_1",
        share_token="tok",
        tenant_id="tenant_demo",
        agent_id="agent_1",
        model_config_id="model_1",
        visitor_id="guest_1",
    )
    # 普通登录令牌不带 share claim
    assert share_scope_from_payload({"user_id": "user_owner", "tenant_id": "tenant_demo"}) is None


def test_share_scope_path_whitelist() -> None:
    assert share_scope_allows_path("/api/chat/stream")
    assert share_scope_allows_path("/api/chat/attachments")
    assert share_scope_allows_path("/api/public/agent-shares/tok/session")
    assert not share_scope_allows_path("/api/enterprise/agents")
    assert not share_scope_allows_path("/api/enterprise/accounts")
    assert not share_scope_allows_path("/api/model-configs")


def test_apply_share_scope_locks_agent_and_model() -> None:
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        agent_id="agent_attacker",
        model_config_id="model_attacker",
        message="hi",
    )
    scope = ShareScope(
        share_id="agentshare_1",
        share_token="tok",
        tenant_id="tenant_demo",
        agent_id="agent_1",
        model_config_id="model_1",
        visitor_id="guest_1",
    )
    locked = _apply_share_scope(request, scope)
    assert locked.agent_id == "agent_1"
    assert locked.model_config_id == "model_1"

    # 非分享请求原样返回
    assert _apply_share_scope(request, None) is request


def test_apply_share_scope_ignores_fastapi_dependency_default() -> None:
    """直接调用 API 函数（单测 / 内部复用）时 share_scope 的默认值是 FastAPI 的 Depends 对象。

    早先写成 `share_scope is not None` 会把普通请求误判成分享访客，
    花名册因此被收敛成空列表（test_agent_permissions 里那条例外就是这么暴露的）。
    """
    from fastapi.params import Depends as DependsParam

    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        agent_id="agent_x",
        model_config_id="model_x",
        message="hi",
    )
    default_value = DependsParam(lambda: None)
    assert _apply_share_scope(request, default_value) is request  # type: ignore[arg-type]


def test_ensure_tenant_missing_raises() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        with pytest.raises(HTTPException):
            ensure_tenant(db, "tenant_demo")


def test_user_list_hides_share_guests() -> None:
    """分享访客（source=share）不是租户成员，不能出现在用户管理列表/处理人选择器里。"""
    from app.api.auth import list_users

    engine = _test_engine()
    _seed(engine)
    share = _create(engine)
    with Session(engine) as db:
        open_agent_share_session(share.token, AgentShareVisitorSessionRequest(), db)
        db.add(
            User(
                id="user_admin",
                tenant_id="tenant_demo",
                username="admin",
                role="admin",
                password_hash="",
            )
        )
        db.commit()

    # 管理员：默认看不到访客
    with Session(engine) as db:
        admin = db.get(User, "user_admin")
        names = [row.username for row in list_users("tenant_demo", current_user=admin, db=db)]
    assert {"owner", "other", "admin"} <= set(names)
    assert not any(name.startswith("share_guest_") for name in names)

    # 管理员显式打开 include_guests 才看得到（供排查用）
    with Session(engine) as db:
        admin = db.get(User, "user_admin")
        rows = list_users("tenant_demo", include_guests=True, current_user=admin, db=db)
    guests = [row for row in rows if row.username.startswith("share_guest_")]
    assert len(guests) == 1
    assert guests[0].source == "share"

    # 普通成员即使传 include_guests 也拿不到访客
    with Session(engine) as db:
        member = db.get(User, "user_owner")
        rows = list_users(
            "tenant_demo", include_guests=True, current_user=member, db=db
        )
    assert not any(row.username.startswith("share_guest_") for row in rows)
