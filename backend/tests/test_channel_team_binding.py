from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select
from sqlmodel import create_engine as sqlmodel_create_engine

import app.channels.service_intake as intake_module
import app.core.agent_loop as agent_loop_module
from app.api import channels as channels_api
from app.channels.service_intake import process_inbound
from app.db import database, get_session
from app.db.models import (
    AgentProfile,
    ChannelBinding,
    ChannelBindingAgent,
    ChannelDelivery,
    ChannelInboundEvent,
    ChatSession,
    Message,
    Team,
    TeamTask,
    Tenant,
    User,
    new_id,
)
from app.security.auth import create_access_token
from app.teams import wakeup
from app.teams.service import add_member, create_team, set_leader


def _test_engine():
    engine = sqlmodel_create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _p2p_message(event_id: str = "evt_1", text: str = "你好") -> dict:
    return {
        "message_id": event_id,
        "from_user_id": "user_ab12cd34@im.wechat",
        "to_user_id": "bot_1@im.bot",
        "client_id": f"wx-{event_id}",
        "session_id": "user_ab12cd34@im.wechat#bot_1@im.bot",
        "message_type": 1,
        "message_state": 2,
        "context_token": f"ctx_{event_id}",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


def _seed_team(db: Session, *, with_leader: bool = True) -> Team:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(AgentProfile(id="agent_tl", tenant_id="tenant_demo", name="TL 小队长"))
    db.add(AgentProfile(id="agent_worker", tenant_id="tenant_demo", name="工人甲"))
    db.add(AgentProfile(id="agent_worker2", tenant_id="tenant_demo", name="工人乙"))
    db.commit()
    team = create_team(
        db,
        tenant_id="tenant_demo",
        name="增长团队",
        description=None,
        owner_user_id="user_admin",
    )
    if with_leader:
        add_member(db, team, agent_id="agent_tl", role="leader")
    add_member(db, team, agent_id="agent_worker")
    add_member(db, team, agent_id="agent_worker2")
    return team


def _seed_team_binding(engine, team_id: str | None, **overrides) -> str:
    with Session(engine) as db:
        values = {
            "tenant_id": "tenant_demo",
            "agent_id": "agent_tl",
            "channel": "wechat",
            "status": "active",
            "config_json": {"ilink_bot_id": "bot_1@im.bot"},
            "team_id": team_id,
        }
        values.update(overrides)
        binding = ChannelBinding(**values)
        db.add(binding)
        db.commit()
        return binding.id


def _load_binding(engine, binding_id: str) -> ChannelBinding:
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        db.expunge(binding)
        return binding


class RecordingAgentLoop:
    """替代真实 AgentLoop：记录请求、模拟消息落库并返回固定回复。"""

    calls: list = []
    reply_text = "自动回复"

    def __init__(self, db):
        self.db = db

    def handle_turn(self, request):
        type(self).calls.append(request)
        self.db.add(
            Message(
                id=new_id("msg"),
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                role="user",
                content=request.message,
                metadata_json={"client_turn_id": request.client_turn_id or ""},
            )
        )
        self.db.add(
            Message(
                id=new_id("msg"),
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                role="assistant",
                content=type(self).reply_text,
                metadata_json={},
            )
        )
        self.db.commit()
        return SimpleNamespace(reply=type(self).reply_text, session_id=request.session_id)


@pytest.fixture(autouse=True)
def _fake_agent_loop(monkeypatch):
    RecordingAgentLoop.calls = []
    RecordingAgentLoop.reply_text = "自动回复"
    monkeypatch.setattr(agent_loop_module, "AgentLoop", RecordingAgentLoop)
    monkeypatch.setattr(
        intake_module, "_send_wechat_typing", lambda *args, **kwargs: None
    )
    yield


# ---------- 入站:团队绑定直路由 TL ----------


def test_team_binding_inbound_routes_to_tl() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        team = _seed_team(db)
        team_id = team.id
    binding_id = _seed_team_binding(engine, team_id)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_1"), db_engine=engine) is True
    assert len(RecordingAgentLoop.calls) == 1
    request = RecordingAgentLoop.calls[0]
    # 团队分支:interaction_mode=team_tl,消息被 build_tl_chat_message 包装(含花名册)
    assert request.interaction_mode == "team_tl"
    assert request.agent_id == "agent_tl"
    assert "团队「增长团队」的 TL" in request.message
    assert "团队花名册:" in request.message
    assert "人的需求:你好" in request.message

    with Session(engine) as db:
        chat_session = db.get(ChatSession, request.session_id)
        # 会话落 team_id 且标题命中 TL 对话识别三条件之一
        assert chat_session.team_id == team_id
        assert chat_session.title == "团队 增长团队 · TL 对话"
        assert chat_session.channel_binding_id == binding_id


def test_team_binding_follows_leader_change() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        team = _seed_team(db)
        team_id = team.id
    binding_id = _seed_team_binding(engine, team_id)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_1"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[0].agent_id == "agent_tl"

    # 换帅:新消息路由给新 TL,并按新 agent_id 锚定另起团队会话
    with Session(engine) as db:
        team = db.get(Team, team_id)
        set_leader(db, team, "agent_worker2")
    assert process_inbound(binding, _p2p_message("evt_2", "新需求"), db_engine=engine) is True
    assert len(RecordingAgentLoop.calls) == 2
    request = RecordingAgentLoop.calls[1]
    assert request.agent_id == "agent_worker2"
    assert request.interaction_mode == "team_tl"
    assert request.session_id != RecordingAgentLoop.calls[0].session_id

    with Session(engine) as db:
        new_session = db.get(ChatSession, request.session_id)
        assert new_session.team_id == team_id
        assert new_session.agent_id == "agent_worker2"
        assert "TL 对话" in (new_session.title or "")


def test_team_binding_tl_reply_creates_tasks(monkeypatch) -> None:
    engine = _test_engine()
    with Session(engine) as db:
        team = _seed_team(db)
        team_id = team.id
    binding_id = _seed_team_binding(engine, team_id)
    binding = _load_binding(engine, binding_id)

    started: list[str] = []
    monkeypatch.setattr(wakeup, "start_wakeup_async", started.append)
    RecordingAgentLoop.reply_text = (
        "好的，马上安排。\n"
        "```json\n"
        '{"team_tasks": [{"title": "调研竞品", "description": "输出调研报告", '
        '"assignee_agent_id": "agent_worker"}]}\n'
        "```"
    )

    assert process_inbound(binding, _p2p_message("evt_1", "帮我调研一下竞品"), db_engine=engine) is True

    with Session(engine) as db:
        tasks = db.exec(select(TeamTask).where(TeamTask.team_id == team_id)).all()
        assert len(tasks) == 1
        assert tasks[0].title == "调研竞品"
        assert tasks[0].assignee_agent_id == "agent_worker"
        assert tasks[0].created_by_tl is True
    # 直派任务触发成员唤醒(测试里同步收集,不起线程)
    assert len(started) == 1


def test_team_binding_without_leader_replies_notice() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        team = _seed_team(db, with_leader=False)
        team_id = team.id
    binding_id = _seed_team_binding(engine, team_id)
    binding = _load_binding(engine, binding_id)

    # 团队未设 TL:回复提示文案,不进 AgentLoop
    assert process_inbound(binding, _p2p_message("evt_1"), db_engine=engine) is False
    assert RecordingAgentLoop.calls == []

    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "done"
        notices = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "notice")
        ).all()
        assert len(notices) == 1
        assert "暂未设置 TL" in notices[0].text


def test_team_binding_switch_command_rejected() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        team = _seed_team(db)
        team_id = team.id
    binding_id = _seed_team_binding(engine, team_id)
    binding = _load_binding(engine, binding_id)

    # 员工切换类指令在团队绑定下不可用,只回提示不进对话
    assert process_inbound(binding, _p2p_message("evt_1", "/员工"), db_engine=engine) is False
    assert RecordingAgentLoop.calls == []

    with Session(engine) as db:
        notices = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "notice")
        ).all()
        assert len(notices) == 1
        assert "团队 TL" in notices[0].text


# ---------- 创建/更新绑定 API ----------


def _make_client(engine):
    app = FastAPI()
    app.include_router(channels_api.router)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return TestClient(app)


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user)}"}


def _seed_api_users(engine) -> dict[str, User]:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(Tenant(id="tenant_other", name="Other"))
        admin = User(
            id="user_admin", tenant_id="tenant_demo", username="admin",
            role="admin", password_hash="x",
        )
        member = User(
            id="user_member", tenant_id="tenant_demo", username="member", password_hash="x",
        )
        db.add(AgentProfile(id="agent_tl", tenant_id="tenant_demo", name="TL 小队长"))
        db.add(AgentProfile(id="agent_worker", tenant_id="tenant_demo", name="工人甲"))
        db.add(AgentProfile(id="agent_outside", tenant_id="tenant_other", name="外部 TL"))
        db.add_all([admin, member])
        db.commit()
        for user in (admin, member):
            db.refresh(user)
            db.expunge(user)
        return {"admin": admin, "member": member}


def _create_api_team(engine, *, tenant_id: str = "tenant_demo", with_leader: bool = True) -> str:
    with Session(engine) as db:
        team = create_team(
            db,
            tenant_id=tenant_id,
            name=f"团队-{tenant_id}",
            description=None,
            owner_user_id="user_admin",
        )
        leader_id = "agent_tl" if tenant_id == "tenant_demo" else "agent_outside"
        if with_leader:
            add_member(db, team, agent_id=leader_id, role="leader")
        return team.id


def test_create_team_binding_success() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    team_id = _create_api_team(engine)
    client = _make_client(engine)

    response = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "team_id": team_id, "channel": "wechat"},
        headers=_auth(users["admin"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["team_id"] == team_id
    assert payload["team_name"] == "团队-tenant_demo"
    # 遗留 agent_id 列回写现任 TL
    assert payload["agent_id"] == "agent_tl"

    with Session(engine) as db:
        # 团队绑定不写员工挂载行
        mounts = db.exec(
            select(ChannelBindingAgent).where(
                ChannelBindingAgent.binding_id == payload["id"]
            )
        ).all()
        assert mounts == []


def test_create_binding_agent_and_team_mutually_exclusive() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    team_id = _create_api_team(engine)
    client = _make_client(engine)

    both = client.post(
        "/api/enterprise/channels",
        json={
            "tenant_id": "tenant_demo",
            "agent_id": "agent_tl",
            "team_id": team_id,
            "channel": "wechat",
        },
        headers=_auth(users["admin"]),
    )
    assert both.status_code == 400

    neither = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "channel": "wechat"},
        headers=_auth(users["admin"]),
    )
    assert neither.status_code == 400


def test_create_team_binding_requires_leader() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    team_id = _create_api_team(engine, with_leader=False)
    client = _make_client(engine)

    response = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "team_id": team_id, "channel": "wechat"},
        headers=_auth(users["admin"]),
    )
    assert response.status_code == 400
    assert "TL" in response.json()["detail"]


def test_create_team_binding_rejects_cross_tenant_team() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    team_id = _create_api_team(engine, tenant_id="tenant_other")
    client = _make_client(engine)

    response = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "team_id": team_id, "channel": "wechat"},
        headers=_auth(users["admin"]),
    )
    assert response.status_code == 404


def test_create_team_binding_requires_tl_manager_permission() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    team_id = _create_api_team(engine)
    client = _make_client(engine)

    # 普通成员不是 TL 员工的管理者:复用员工绑定同款守卫,拒绝创建
    response = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "team_id": team_id, "channel": "wechat"},
        headers=_auth(users["member"]),
    )
    assert response.status_code == 403


def test_update_team_binding_rejects_agents_replacement() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    team_id = _create_api_team(engine)
    client = _make_client(engine)

    created = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "team_id": team_id, "channel": "wechat"},
        headers=_auth(users["admin"]),
    )
    assert created.status_code == 200
    binding_id = created.json()["id"]

    response = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_worker", "is_default": True}]},
        headers=_auth(users["admin"]),
    )
    assert response.status_code == 400
    assert "团队绑定" in response.json()["detail"]


# ---------- SQLite 迁移 ----------


def test_channel_binding_team_id_migration_is_idempotent(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "migrate.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        # 老库 channel_bindings 无 team_id 列
        conn.execute(
            text(
                """
                CREATE TABLE channel_bindings (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR,
                    agent_id VARCHAR,
                    channel VARCHAR,
                    status VARCHAR,
                    credentials_enc VARCHAR,
                    config_json JSON,
                    connected BOOLEAN,
                    created_by_user_id VARCHAR,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )

    monkeypatch.setattr(database, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_sqlite_skill_schema()
    columns = {column["name"] for column in inspect(engine).get_columns("channel_bindings")}
    assert "team_id" in columns

    # 重复执行不炸(列已存在)
    database._migrate_sqlite_skill_schema()
