from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_open_gallery_binding
from app.api.tool_access import (
    ToolAccessRequestCreate,
    ToolAccessRequestReview,
    approve_tool_access_request,
    create_tool_access_request,
    list_notifications,
    list_tool_access_requests,
    reject_tool_access_request,
)
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    Tenant,
    Tool,
    ToolAccessRequest,
    User,
    UserNotification,
)


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _admin() -> User:
    return User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash="test",
    )


def _member() -> User:
    return User(
        id="user_owner",
        tenant_id="tenant_demo",
        username="owner",
        role="member",
        password_hash="test",
    )


def _seed(db: Session) -> tuple[Tool, AgentProfile]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        AgentProfile(
            id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
        )
    )
    db.add(
        AgentProfile(
            id="agent_requester",
            tenant_id="tenant_demo",
            name="申请人的员工",
            metadata_json={"owner_user_id": "user_requester"},
        )
    )
    tool = Tool(
        id="tool_weather",
        tenant_id="tenant_demo",
        name="weather.lookup",
        display_name="天气查询",
        description="查询城市天气",
        method="POST",
        url="https://example.test/weather",
    )
    db.add(tool)
    db.commit()
    ensure_open_gallery_binding(
        db,
        "tenant_demo",
        "tool",
        tool.id,
        "active",
        metadata_json={"owner_user_id": "user_owner", "created_by_user_id": "user_owner"},
    )
    db.commit()
    agent = db.get(AgentProfile, "agent_requester")
    assert agent is not None
    return tool, agent


def test_request_approval_binds_tool_inactive_and_notifies_requester() -> None:
    with _test_session() as db:
        tool, agent = _seed(db)
        requester = User(
            id="user_requester",
            tenant_id="tenant_demo",
            username="requester",
            role="member",
            password_hash="test",
        )

        request = create_tool_access_request(
            tool.id,
            ToolAccessRequestCreate(tenant_id="tenant_demo", agent_id=agent.id, note="需要查询天气"),
            db,
            requester,
        )
        assert request.status == "pending"
        assert request.tool_display_name == "天气查询"
        assert request.review_note == "需要查询天气"

        pending = list_tool_access_requests("tenant_demo", "pending", db, _admin())
        assert [row.id for row in pending] == [request.id]

        approved = approve_tool_access_request(
            request.id,
            ToolAccessRequestReview(tenant_id="tenant_demo"),
            db,
            _admin(),
        )
        assert approved.status == "approved"
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == tool.id,
            )
        ).one()
        assert binding.status == "inactive"

        notifications = list_notifications("tenant_demo", False, 50, db, requester)
        assert len(notifications) == 1
        assert notifications[0].kind == "tool_access_approved"
        assert notifications[0].read is False


def test_reject_creates_message_and_does_not_bind_tool() -> None:
    with _test_session() as db:
        tool, agent = _seed(db)
        requester = User(
            id="user_requester",
            tenant_id="tenant_demo",
            username="requester",
            role="member",
            password_hash="test",
        )
        request = create_tool_access_request(
            tool.id,
            ToolAccessRequestCreate(tenant_id="tenant_demo", agent_id=agent.id, note="需要查询天气"),
            db,
            requester,
        )

        rejected = reject_tool_access_request(
            request.id,
            ToolAccessRequestReview(tenant_id="tenant_demo", note="暂不开放"),
            db,
            _admin(),
        )
        assert rejected.status == "rejected"
        assert db.get(ToolAccessRequest, request.id).review_note == "暂不开放"
        assert db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == tool.id,
            )
        ).first() is None
        notifications = db.exec(
            select(UserNotification).where(UserNotification.user_id == requester.id)
        ).all()
        assert [row.kind for row in notifications] == ["tool_access_rejected"]
