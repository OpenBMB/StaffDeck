"""数字员工使用次数：Public API 新建会话也要计数。

口径：每个**新建的会话**算一次（站内 / 分享访客 / Public API 一致），
同一会话的多轮对话、以及用同一个 external_session_id 复用会话都不重复计。
"""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.agents.usage import bump_agent_usage
from app.db.models import AgentProfile, Tenant, User
from app.public_api.auth import PublicPrincipal
from app.public_api.schemas import PublicSessionCreate
from app.public_api.sessions import create_public_session_row
from app.security.auth import hash_password

TENANT = "tenant_demo"
AGENT = "agent_demo"


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session) -> PublicPrincipal:
    db.add(Tenant(id=TENANT, name="Demo"))
    user = User(
        id="user_api",
        tenant_id=TENANT,
        username="api_actor",
        password_hash=hash_password("x"),
        role="member",
    )
    db.add(user)
    db.add(
        AgentProfile(
            id=AGENT,
            tenant_id=TENANT,
            name="重庆 AI G.O",
            status="active",
            usage_count=0,
        )
    )
    db.commit()
    db.refresh(user)
    return PublicPrincipal(
        tenant_id=TENANT,
        actor_user=user,
        scopes=frozenset({"sessions:write"}),
        credential_id="cred_1",
    )


def _usage(db: Session) -> int:
    db.expire_all()
    row = db.get(AgentProfile, AGENT)
    assert row is not None
    return int(row.usage_count or 0)


def test_bump_agent_usage_increments() -> None:
    db = _session()
    _seed(db)

    bump_agent_usage(db, AGENT)
    db.commit()

    assert _usage(db) == 1


def test_public_api_session_counts_once_per_new_session() -> None:
    db = _session()
    principal = _seed(db)

    create_public_session_row(
        db,
        principal,
        AGENT,
        PublicSessionCreate(external_session_id="ext-1"),
    )
    assert _usage(db) == 1

    # 同一个 external_session_id 再来一次：复用已有会话，不再计数
    create_public_session_row(
        db,
        principal,
        AGENT,
        PublicSessionCreate(external_session_id="ext-1"),
    )
    assert _usage(db) == 1

    # 新的外部会话 = 新会话 = 再计一次
    create_public_session_row(
        db,
        principal,
        AGENT,
        PublicSessionCreate(external_session_id="ext-2"),
    )
    assert _usage(db) == 2
