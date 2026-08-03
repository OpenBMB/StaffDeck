from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.feedback import get_feedback_session_detail, get_feedback_summary, list_feedback_sessions
from app.api.sessions import get_session_detail, list_sessions
from app.db.models import AgentEvent, ChatSession, Message, MessageFeedback, Tenant, User


def test_enterprise_sessions_are_limited_to_current_user_even_for_admin() -> None:
    with _test_session() as db:
        admin, _other = _seed_session_privacy_rows(db)

        rows = list_sessions("tenant_demo", agent_id=None, current_user=admin, db=db)

        assert [row["id"] for row in rows] == ["session_admin"]

        # admin 现在可查看任意会话详情（admin/agent 创建者放开规则，详见 test_enterprise_session_visibility.py）
        detail = get_session_detail("session_other", "tenant_demo", current_user=admin, db=db)
        assert detail["session"]["id"] == "session_other"


def test_enterprise_feedback_lists_own_sessions_without_scope_but_admin_can_open_detail() -> None:
    with _test_session() as db:
        admin, _other = _seed_session_privacy_rows(db)
        _seed_feedback_rows(db)

        summary = get_feedback_summary("tenant_demo", agent_id=None, limit=1000, current_user=admin, db=db)
        down_rows = list_feedback_sessions("tenant_demo", "down", agent_id=None, limit=200, current_user=admin, db=db)
        detail = get_feedback_session_detail("session_admin", "tenant_demo", current_user=admin, db=db)

        assert summary["down_count"] == 1
        assert [row["session_id"] for row in down_rows] == ["session_admin"]
        assert detail["session"]["id"] == "session_admin"

        scoped_summary = get_feedback_summary(
            "tenant_demo",
            agent_id="agent_shared",
            limit=1000,
            current_user=admin,
            db=db,
        )
        scoped_rows = list_feedback_sessions(
            "tenant_demo",
            "down",
            agent_id="agent_shared",
            limit=200,
            current_user=admin,
            db=db,
        )
        assert scoped_summary["down_count"] == 2
        assert {row["session_id"] for row in scoped_rows} == {
            "session_admin",
            "session_other",
        }

        other_detail = get_feedback_session_detail(
            "session_other", "tenant_demo", current_user=admin, db=db
        )
        assert other_detail["session"]["id"] == "session_other"
        enterprise_detail = get_session_detail(
            "session_other", "tenant_demo", current_user=admin, db=db
        )
        assert enterprise_detail["session"]["session_display_name"] == "其他用户昵称"
        assert enterprise_detail["feedback"][0]["id"] == "feedback_other"
        assert enterprise_detail["messages"][-1]["feedback_rating"] == "down"
        assert enterprise_detail["traces"]
        trace = enterprise_detail["traces"][0]
        assert trace["duration_ms"] == 3000
        assert trace["model_duration_ms"] == 2000
        assert trace["model_call_count"] == 1


def _seed_session_privacy_rows(db: Session) -> tuple[User, User]:
    started_at = datetime(2026, 8, 2, 10, 0, 0)
    db.add(Tenant(id="tenant_demo", name="Demo"))
    admin = User(id="admin_user", tenant_id="tenant_demo", username="admin", role="admin", password_hash="x")
    other = User(
        id="other_user",
        tenant_id="tenant_demo",
        username="other",
        display_name="其他用户昵称",
        password_hash="x",
    )
    db.add(admin)
    db.add(other)
    db.add(
        ChatSession(
            id="session_admin",
            tenant_id="tenant_demo",
            user_id=admin.id,
            agent_id="agent_shared",
            title="管理员自己的会话",
        )
    )
    db.add(
        ChatSession(
            id="session_other",
            tenant_id="tenant_demo",
            user_id=other.id,
            agent_id="agent_shared",
            title="其他用户的会话",
        )
    )
    db.add(
        Message(
            id="msg_admin_user",
            tenant_id="tenant_demo",
            session_id="session_admin",
            role="user",
            content="admin question",
            created_at=started_at,
        )
    )
    db.add(
        Message(
            id="msg_admin_assistant",
            tenant_id="tenant_demo",
            session_id="session_admin",
            role="assistant",
            content="admin answer",
            created_at=started_at + timedelta(milliseconds=3000),
        )
    )
    db.add(
        Message(
            id="msg_other_user",
            tenant_id="tenant_demo",
            session_id="session_other",
            role="user",
            content="other question",
            created_at=started_at,
        )
    )
    db.add(
        Message(
            id="msg_other_assistant",
            tenant_id="tenant_demo",
            session_id="session_other",
            role="assistant",
            content="other answer",
            created_at=started_at + timedelta(milliseconds=3000),
        )
    )
    db.add_all(
        [
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_admin",
                event_type="user_message_received",
                payload_json={"message_id": "msg_admin_user", "message": "admin question"},
                created_at=started_at,
            ),
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_admin",
                event_type="llm_call_finished",
                payload_json={
                    "turn_id": "msg_admin_user",
                    "operation": "response.generate",
                    "started_at": (started_at + timedelta(milliseconds=500)).isoformat(),
                    "duration_ms": 2000,
                },
                created_at=started_at + timedelta(milliseconds=2500),
            ),
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_admin",
                event_type="assistant_message_created",
                payload_json={
                    "message_id": "msg_admin_assistant",
                    "turn_id": "msg_admin_user",
                    "user_message_id": "msg_admin_user",
                },
                created_at=started_at + timedelta(milliseconds=3000),
            ),
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_other",
                event_type="user_message_received",
                payload_json={"message_id": "msg_other_user", "message": "other question"},
                created_at=started_at,
            ),
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_other",
                event_type="llm_call_finished",
                payload_json={
                    "turn_id": "msg_other_user",
                    "operation": "response.generate",
                    "started_at": (started_at + timedelta(milliseconds=500)).isoformat(),
                    "duration_ms": 2000,
                },
                created_at=started_at + timedelta(milliseconds=2500),
            ),
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_other",
                event_type="assistant_message_created",
                payload_json={
                    "message_id": "msg_other_assistant",
                    "turn_id": "msg_other_user",
                    "user_message_id": "msg_other_user",
                },
                created_at=started_at + timedelta(milliseconds=3000),
            ),
        ]
    )
    db.commit()
    return admin, other


def _seed_feedback_rows(db: Session) -> None:
    db.add(
        MessageFeedback(
            id="feedback_admin",
            tenant_id="tenant_demo",
            session_id="session_admin",
            message_id="msg_admin_assistant",
            user_id="admin_user",
            rating="down",
            analysis_bucket="model_issue",
        )
    )
    db.add(
        MessageFeedback(
            id="feedback_other",
            tenant_id="tenant_demo",
            session_id="session_other",
            message_id="msg_other_assistant",
            user_id="other_user",
            rating="down",
            analysis_bucket="skill_issue",
        )
    )
    db.commit()


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
