from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select
from test_teams_api import (
    _admin_user,
    _member_user,
    _seed_team,
    _stub_start_wakeup,
    _test_session,
)

from app.api import chat as chat_api
from app.api import teams as teams_api
from app.core import AgentLoop
from app.db import database
from app.db.models import ChatSession, TeamTask, TeamWakeEvent, User
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse, SessionPublic
from app.teams.schema import TeamTLChatRequest, TeamTLSessionRequest
from app.teams.service import create_team

# ---------- sessions.team_id 迁移 ----------


def test_sessions_team_id_migration(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """老库无 team_id 列:migrate 后存在,且重复执行幂等。"""
    db_path = tmp_path / "migrate.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE sessions (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR,
                    user_id VARCHAR,
                    agent_id VARCHAR,
                    title VARCHAR,
                    status VARCHAR
                )
                """
            )
        )
    monkeypatch.setattr(database, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_sqlite_skill_schema()
    columns = {column["name"] for column in inspect(engine).get_columns("sessions")}
    assert "team_id" in columns

    database._migrate_sqlite_skill_schema()  # 重复执行不炸
    columns = {column["name"] for column in inspect(engine).get_columns("sessions")}
    assert "team_id" in columns


# ---------- TL 会话 get-or-create ----------


def test_tl_session_get_or_create_idempotent() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        admin = _admin_user()

        first = teams_api.tl_session_endpoint(
            team.id, TeamTLSessionRequest(tenant_id="tenant_demo"), db, admin
        )
        session = db.get(ChatSession, first.session_id)
        assert session is not None
        assert session.team_id == team.id
        assert session.agent_id == "agent_tl"
        assert session.user_id == "user_admin"

        second = teams_api.tl_session_endpoint(
            team.id, TeamTLSessionRequest(tenant_id="tenant_demo"), db, admin
        )
        assert second.session_id == first.session_id
        sessions = db.exec(
            select(ChatSession).where(ChatSession.team_id == team.id)
        ).all()
        assert len(sessions) == 1


def test_tl_session_requires_leader() -> None:
    with _test_session() as db:
        _seed_team(db)  # 种子租户/员工
        bare = create_team(
            db,
            tenant_id="tenant_demo",
            name="无TL团队",
            description=None,
            owner_user_id="user_admin",
        )
        with pytest.raises(HTTPException) as exc_info:
            teams_api.tl_session_endpoint(
                bare.id, TeamTLSessionRequest(tenant_id="tenant_demo"), db, _admin_user()
            )
        assert exc_info.value.status_code == 400


def test_tl_session_open_to_tenant_member_and_rejects_foreign_tenant() -> None:
    """tl/session 对本租户所有登录用户开放(对齐 TL chat 权限),外租户拒绝。"""
    with _test_session() as db:
        team = _seed_team(db)
        response = teams_api.tl_session_endpoint(
            team.id, TeamTLSessionRequest(tenant_id="tenant_demo"), db, _member_user()
        )
        session = db.get(ChatSession, response.session_id)
        assert session is not None
        assert session.team_id == team.id
        assert session.user_id == "user_member"

        foreign = User(
            id="user_foreign",
            tenant_id="tenant_other",
            username="foreign",
            role="admin",
            password_hash="test",
        )
        with pytest.raises(HTTPException) as exc_info:
            teams_api.tl_session_endpoint(
                team.id, TeamTLSessionRequest(tenant_id="tenant_demo"), db, foreign
            )
        assert exc_info.value.status_code == 403


# ---------- 会话 read 带团队归属 ----------


def test_session_read_carries_team_id_and_name() -> None:
    """团队 TL 会话 read 带 team_id/team_name;普通会话两者均为 None。"""
    with _test_session() as db:
        team = _seed_team(db)
        admin = _admin_user()
        plain = ChatSession(
            id="session_plain_read",
            tenant_id="tenant_demo",
            user_id=admin.id,
            agent_id="agent_tl",
            title="普通会话",
            status="active",
        )
        db.add(plain)
        db.commit()
        tl = teams_api.tl_session_endpoint(
            team.id, TeamTLSessionRequest(tenant_id="tenant_demo"), db, admin
        )

        reads = {item.id: item for item in chat_api.list_chat_sessions("tenant_demo", admin, db)}
        assert reads[tl.session_id].team_id == team.id
        assert reads[tl.session_id].team_name == team.name
        assert reads[plain.id].team_id is None
        assert reads[plain.id].team_name is None


def test_tl_chat_session_carries_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        _stub_start_wakeup(monkeypatch)

        def fake_handle_turn(self, request):
            return ChatTurnResponse(
                reply="收到,先讨论。",
                session_id=request.session_id,
                session_state=SessionPublic(
                    session_id=request.session_id, tenant_id=request.tenant_id
                ),
            )

        monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)
        response = teams_api.tl_chat_endpoint(
            team.id,
            TeamTLChatRequest(tenant_id="tenant_demo", message="聊聊规划"),
            db,
            _admin_user(),
        )
        session = db.get(ChatSession, response.session_id)
        assert session is not None
        assert session.team_id == team.id


# ---------- 主聊天端团队 TL 会话 ----------


def _make_tl_session(db: Session, team, *, team_id: str | None) -> ChatSession:
    session = ChatSession(
        id="session_tl",
        tenant_id="tenant_demo",
        user_id="user_admin",
        agent_id="agent_tl",
        title="团队 TL 对话",
        status="active",
        team_id=team_id,
    )
    db.add(session)
    db.commit()
    return session


def test_chat_turn_team_tl_session_injects_context_and_creates_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        session = _make_tl_session(db, team, team_id=team.id)
        started = _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(
            chat_api, "_schedule_session_title_summary", lambda *a, **kw: None
        )
        seen: dict[str, str] = {}

        def fake_handle_turn(self, request):
            seen["message"] = request.message
            seen["interaction_mode"] = request.interaction_mode
            reply = (
                "收到,派给 Worker。\n"
                '```json\n{"team_tasks": [{"title": "竞品调研", '
                '"assignee_agent_id": "agent_worker"}]}\n```'
            )
            return ChatTurnResponse(
                reply=reply,
                session_id=request.session_id,
                session_state=SessionPublic(
                    session_id=request.session_id, tenant_id=request.tenant_id
                ),
            )

        monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)

        response = chat_api.chat_turn(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                session_id=session.id,
                client_turn_id="ct_1",
                message="帮我调研竞品",
            ),
            _admin_user(),
            db,
        )

        # 消息被团队上下文包装后再走引擎
        assert "团队花名册" in seen["message"]
        assert "agent_worker" in seen["message"]
        assert "人的需求:帮我调研竞品" in seen["message"]
        assert seen["interaction_mode"] == "team_tl"
        assert response.reply.startswith("收到")

        # 回复后处理:派任务块解析并创建任务 + 唤醒(与 tl_chat 端点同语义)
        tasks = db.exec(select(TeamTask).where(TeamTask.team_id == team.id)).all()
        assert len(tasks) == 1
        assert tasks[0].title == "竞品调研"
        assert tasks[0].assignee_agent_id == "agent_worker"
        assert tasks[0].created_by_tl is True
        wakes = db.exec(select(TeamWakeEvent)).all()
        assert len(wakes) == 1
        assert wakes[0].trigger_type == "task_assigned"
        assert started == [wakes[0].id]


def test_chat_turn_plain_session_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """非团队会话:不注入团队上下文、不做派任务后处理。"""
    with _test_session() as db:
        team = _seed_team(db)
        session = ChatSession(
            id="session_plain",
            tenant_id="tenant_demo",
            user_id="user_admin",
            agent_id="agent_tl",  # 同一 agent,但会话未挂 team_id
            title="普通会话",
            status="active",
        )
        db.add(session)
        db.commit()
        monkeypatch.setattr(
            chat_api, "_schedule_session_title_summary", lambda *a, **kw: None
        )
        seen: dict[str, str] = {}

        def fake_handle_turn(self, request):
            seen["message"] = request.message
            seen["interaction_mode"] = request.interaction_mode
            return ChatTurnResponse(
                reply="好的",
                session_id=request.session_id,
                session_state=SessionPublic(
                    session_id=request.session_id, tenant_id=request.tenant_id
                ),
            )

        monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)

        chat_api.chat_turn(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                session_id=session.id,
                client_turn_id="ct_2",
                message="帮我调研竞品",
            ),
            _admin_user(),
            db,
        )

        assert seen["message"] == "帮我调研竞品"
        assert seen["interaction_mode"] == "normal"
        assert db.exec(select(TeamTask).where(TeamTask.team_id == team.id)).all() == []
