from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select
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
from app.db.models import (
    AgentProfile,
    ChatSession,
    Message,
    Team,
    TeamTask,
    TeamWakeEvent,
    Tenant,
    User,
)
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse, SessionPublic
from app.teams.schema import TeamTLChatRequest, TeamTLSessionRequest
from app.teams.service import create_team, set_leader

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


def test_purge_orphaned_chat_sessions_cleans_pre_fix_leftovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """级联清理上线前删除团队/员工残留的孤儿会话,启动时一次性清掉。"""
    db_path = tmp_path / "orphans.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(AgentProfile(id="agent_keep", tenant_id="tenant_demo", name="保留员工"))
        team = Team(
            tenant_id="tenant_demo",
            name="存活的团队",
            owner_user_id="user_admin",
            status="active",
        )
        db.add(team)
        for session_id, team_id, agent_id, title in (
            ("session_team_orphan", "team_gone", "agent_keep", "团队 已删团队 · TL 对话"),
            ("session_agent_orphan", None, "agent_gone", "与已删员工的对话"),
            ("session_team_ok", team.id, "agent_keep", "团队 存活的团队 · TL 对话"),
            ("session_plain_ok", None, "agent_keep", "普通会话"),
            ("session_no_agent", None, None, "无员工会话"),
        ):
            db.add(
                ChatSession(
                    id=session_id,
                    tenant_id="tenant_demo",
                    user_id="user_admin",
                    agent_id=agent_id,
                    team_id=team_id,
                    title=title,
                    status="active",
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
        db.commit()

    monkeypatch.setattr(database, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)

    database._purge_orphaned_chat_sessions()  # 首次清理
    database._purge_orphaned_chat_sessions()  # 重复执行幂等

    with Session(engine) as db:
        remaining = {session.id for session in db.exec(select(ChatSession)).all()}
        assert remaining == {"session_team_ok", "session_plain_ok", "session_no_agent"}
        remaining_messages = {
            message.session_id for message in db.exec(select(Message)).all()
        }
        assert remaining_messages == remaining


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


def test_tl_session_survives_leader_change_without_creating_another_room() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        admin = _admin_user()
        first = teams_api.tl_session_endpoint(
            team.id, TeamTLSessionRequest(tenant_id="tenant_demo"), db, admin
        )

        set_leader(db, team, "agent_worker")
        second = teams_api.tl_session_endpoint(
            team.id, TeamTLSessionRequest(tenant_id="tenant_demo"), db, admin
        )

        assert second.session_id == first.session_id
        session = db.get(ChatSession, first.session_id)
        assert session is not None
        assert session.agent_id == "agent_worker"


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


def test_global_list_contains_team_group_but_hides_internal_team_sessions() -> None:
    """对话端同时展示单聊和团队群聊，但不暴露任务执行会话。"""
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
        internal = ChatSession(
            id="session_internal_team",
            tenant_id="tenant_demo",
            user_id=admin.id,
            agent_id="agent_tl",
            title="团队竞标裁决:测试",
            status="active",
            team_id=team.id,
        )
        db.add(internal)
        db.commit()
        tl = teams_api.tl_session_endpoint(
            team.id, TeamTLSessionRequest(tenant_id="tenant_demo"), db, admin
        )

        reads = {item.id: item for item in chat_api.list_chat_sessions("tenant_demo", admin, db)}
        assert reads[tl.session_id].team_id == team.id
        assert reads[tl.session_id].team_name == team.name
        assert internal.id not in reads
        assert reads[plain.id].team_id is None
        assert reads[plain.id].team_name is None

        team_read = chat_api.get_chat_session(tl.session_id, "tenant_demo", admin, db)
        assert team_read.team_id == team.id
        assert team_read.team_name == team.name


def test_delete_team_purges_team_chat_sessions() -> None:
    """删除团队后,团队会话及其消息不再出现在对话端列表。"""
    with _test_session() as db:
        team = _seed_team(db)
        admin = _admin_user()
        plain = ChatSession(
            id="session_plain_survivor",
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
        db.add(
            Message(
                id="msg_team_1",
                tenant_id="tenant_demo",
                session_id=tl.session_id,
                role="user",
                content="团队消息",
            )
        )
        db.commit()

        assert teams_api.delete_team_endpoint(team.id, "tenant_demo", db, admin) == {"ok": True}

        assert db.get(ChatSession, tl.session_id) is None
        assert db.exec(select(Message).where(Message.session_id == tl.session_id)).all() == []
        reads = {item.id for item in chat_api.list_chat_sessions("tenant_demo", admin, db)}
        assert tl.session_id not in reads
        assert plain.id in reads


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
            seen["context_injection"] = request.context_injection
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

        # 可见消息保持原文，团队上下文通过仅供运行时使用的字段注入。
        assert seen["message"] == "帮我调研竞品"
        assert "团队花名册" in seen["context_injection"]
        assert "agent_worker" in seen["context_injection"]
        assert seen["context_injection"].endswith("人的需求:")
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
            seen["context_injection"] = request.context_injection
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
        assert seen["context_injection"] is None
        assert seen["interaction_mode"] == "normal"
        assert db.exec(select(TeamTask).where(TeamTask.team_id == team.id)).all() == []
