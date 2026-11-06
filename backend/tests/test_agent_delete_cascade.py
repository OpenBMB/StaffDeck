from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.agents import delete_agent
from app.core.harness_session_cleanup import harness_session_workspace_path
from app.db.models import (
    AgentProfile,
    ChatSession,
    HumanHandoffRequest,
    Message,
    ScheduledTask,
    Team,
    TeamMember,
    Tenant,
    User,
)


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _admin_user() -> User:
    return User(
        id="user_admin", tenant_id="tenant_demo", username="ops", role="admin", password_hash="test"
    )


def test_delete_agent_purges_sessions_membership_tasks_and_handoffs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除员工要清空其会话/工作区,并同步清理团队成员关系、定时任务与待处理转接。"""
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(AgentProfile(id="agent_gone", tenant_id="tenant_demo", name="已删员工"))
        db.add(AgentProfile(id="agent_keep", tenant_id="tenant_demo", name="保留员工"))
        team = Team(
            tenant_id="tenant_demo",
            name="增长团队",
            owner_user_id="user_admin",
            status="active",
        )
        db.add(team)
        db.add(TeamMember(team_id=team.id, agent_id="agent_gone", role="leader"))
        db.add(TeamMember(team_id=team.id, agent_id="agent_keep"))
        for session_id, agent_id in (
            ("session_gone", "agent_gone"),
            ("session_team_gone", "agent_gone"),
            ("session_keep", "agent_keep"),
        ):
            db.add(
                ChatSession(
                    id=session_id,
                    tenant_id="tenant_demo",
                    user_id="user_admin",
                    agent_id=agent_id,
                    title=f"会话-{session_id}",
                    team_id=team.id if session_id == "session_team_gone" else None,
                )
            )
            db.add(
                Message(
                    id=f"msg_{session_id}",
                    tenant_id="tenant_demo",
                    session_id=session_id,
                    role="user",
                    content="你好",
                )
            )
        db.add(
            ScheduledTask(
                tenant_id="tenant_demo",
                agent_id="agent_gone",
                created_by_user_id="user_admin",
                title="已删员工任务",
                prompt="汇总",
            )
        )
        db.add(
            ScheduledTask(
                tenant_id="tenant_demo",
                agent_id="agent_keep",
                created_by_user_id="user_admin",
                title="保留员工任务",
                prompt="汇总",
            )
        )
        db.add(
            HumanHandoffRequest(
                tenant_id="tenant_demo",
                session_id="session_gone",
                agent_id="agent_gone",
                status="pending",
            )
        )
        db.add(
            HumanHandoffRequest(
                tenant_id="tenant_demo",
                session_id="session_gone",
                agent_id="agent_gone",
                status="answered",
            )
        )
        db.commit()

        workspace = harness_session_workspace_path(
            tenant_id="tenant_demo",
            session_id="session_gone",
        )
        workspace.mkdir(parents=True)
        (workspace / "artifact.txt").write_text("artifact", encoding="utf-8")

        assert (
            delete_agent("agent_gone", tenant_id="tenant_demo", db=db, current_user=_admin_user())
            == {"status": "deleted"}
        )

        assert db.get(AgentProfile, "agent_gone") is None
        assert db.get(ChatSession, "session_gone") is None
        assert db.get(ChatSession, "session_team_gone") is None
        assert db.get(ChatSession, "session_keep") is not None
        assert db.exec(select(Message).where(Message.session_id == "session_gone")).all() == []
        assert db.get(Message, "msg_session_keep") is not None
        assert not workspace.exists()
        assert db.exec(select(TeamMember).where(TeamMember.agent_id == "agent_gone")).all() == []
        kept_members = db.exec(select(TeamMember)).all()
        assert [member.agent_id for member in kept_members] == ["agent_keep"]
        paused_task = db.exec(
            select(ScheduledTask).where(ScheduledTask.agent_id == "agent_gone")
        ).one()
        assert paused_task.status == "paused"
        assert paused_task.next_run_at is None
        kept_task = db.exec(
            select(ScheduledTask).where(ScheduledTask.agent_id == "agent_keep")
        ).one()
        assert kept_task.status == "active"
        handoff_statuses = {
            handoff.status
            for handoff in db.exec(
                select(HumanHandoffRequest).where(HumanHandoffRequest.agent_id == "agent_gone")
            ).all()
        }
        assert handoff_statuses == {"cancelled", "answered"}
