import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.channels.service_intake as intake_module
import app.core.agent_loop as agent_loop_module
from app.channels.service_intake import process_inbound
from app.channels.service_outbox import stage_channel_delivery
from app.channels.service_routing import (
    HELP_TEXT,
    agent_names,
    mounted_agents,
    parse_command,
    resolve_current_agent,
    run_command,
)
from app.db.models import (
    AgentProfile,
    ChannelBinding,
    ChannelBindingAgent,
    ChannelConvState,
    ChannelDelivery,
    ChannelInboundEvent,
    ChatSession,
    Message,
    Tenant,
    User,
    new_id,
)


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_binding(engine, *, mounts: list[tuple[str, str, bool]] | None = None) -> str:
    """创建绑定与两个员工;mounts=(agent_id, name, is_default),None 表示存量绑定(无挂载行)。"""
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(AgentProfile(id="agent_xz", tenant_id="tenant_demo", name="行政", metadata_json={}))
        db.add(AgentProfile(id="agent_cw", tenant_id="tenant_demo", name="财务", metadata_json={}))
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            status="active",
            config_json={"ilink_bot_id": "bot_1@im.bot"},
        )
        db.add(binding)
        db.flush()
        for index, (agent_id, _name, is_default) in enumerate(mounts or []):
            db.add(
                ChannelBindingAgent(
                    tenant_id="tenant_demo",
                    binding_id=binding.id,
                    agent_id=agent_id,
                    is_default=is_default,
                    sort_order=index,
                )
            )
        db.commit()
        return binding.id


def _load_binding(engine, binding_id: str) -> ChannelBinding:
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        db.expunge(binding)
        return binding


def _p2p_message(event_id: str, text: str) -> dict:
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


def _group_message(event_id: str, text: str, group_id: str = "room_123456") -> dict:
    msg = _p2p_message(event_id, text)
    msg["group_id"] = group_id
    msg["session_id"] = group_id
    return msg


class RecordingAgentLoop:
    calls: list = []

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
                content=f"{request.agent_id} 的回复",
                metadata_json={},
            )
        )
        self.db.commit()


@pytest.fixture(autouse=True)
def _fake_agent_loop(monkeypatch):
    RecordingAgentLoop.calls = []
    monkeypatch.setattr(agent_loop_module, "AgentLoop", RecordingAgentLoop)
    monkeypatch.setattr(intake_module, "_send_wechat_typing", lambda *args, **kwargs: None)
    yield


# ---------- 指令解析 ----------


def test_parse_command_non_command() -> None:
    assert parse_command("你好") is None
    assert parse_command("") is None
    assert parse_command("  ") is None
    assert parse_command("说 /员工 的事") is None


def test_parse_command_list() -> None:
    assert parse_command("/员工").kind == "list"
    assert parse_command("/list").kind == "list"
    assert parse_command("  /LIST  ").kind == "list"


def test_parse_command_current_and_help() -> None:
    assert parse_command("/当前").kind == "current"
    assert parse_command("/帮助").kind == "help"
    assert parse_command("/?").kind == "help"
    assert parse_command("/？").kind == "help"


def test_parse_command_switch() -> None:
    cmd = parse_command("/切换 财务")
    assert cmd.kind == "switch" and cmd.query == "财务"
    direct = parse_command("/财务")
    assert direct.kind == "switch" and direct.query == "财务"
    empty = parse_command("/切换")
    assert empty.kind == "switch" and empty.query == ""


def test_parse_command_unknown_slash_goes_help() -> None:
    assert parse_command("/foo bar").kind == "help"
    assert parse_command("/").kind == "help"


# ---------- 挂载集与指针 ----------


def test_mounted_agents_legacy_fallback() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)  # 无挂载行
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        mounts = mounted_agents(db, binding)
        assert [m.agent_id for m in mounts] == ["agent_xz"]
        assert mounts[0].is_default is True


def test_mounted_agents_ordering() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, mounts=[("agent_cw", "财务", False), ("agent_xz", "行政", True)])
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        mounts = mounted_agents(db, binding)
        assert [m.agent_id for m in mounts] == ["agent_cw", "agent_xz"]


def test_resolve_current_agent_creates_pointer_at_default() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, mounts=[("agent_xz", "行政", True), ("agent_cw", "财务", False)])
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        current, reset = resolve_current_agent(db, binding, "wechat_p2p_u1")
        db.commit()
        assert current == "agent_xz"
        assert reset is False
        state = db.exec(select(ChannelConvState)).one()
        assert state.current_agent_id == "agent_xz"


def test_resolve_current_agent_resets_when_unmounted() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, mounts=[("agent_xz", "行政", True), ("agent_cw", "财务", False)])
    with Session(engine) as db:
        db.add(
            ChannelConvState(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                external_conv_id="wechat_p2p_u1",
                current_agent_id="agent_gone",
            )
        )
        db.commit()
        binding = db.get(ChannelBinding, binding_id)
        current, reset = resolve_current_agent(db, binding, "wechat_p2p_u1")
        assert current == "agent_xz"
        assert reset is True


# ---------- 指令执行文本 ----------


def _command_setup(engine):
    binding_id = _seed_binding(
        engine, mounts=[("agent_xz", "行政", True), ("agent_cw", "财务", False)]
    )
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        db.expunge(binding)
        return binding


def test_run_command_list_marks_default_and_current() -> None:
    engine = _test_engine()
    binding = _command_setup(engine)
    with Session(engine) as db:
        reply = run_command(db, binding, "wechat_p2p_u1", parse_command("/员工"))
        lines = reply.splitlines()
        assert lines[0] == "可调度员工："
        assert "1. 行政（默认/当前）" in lines
        assert "2. 财务" in lines


def test_run_command_switch_and_current() -> None:
    engine = _test_engine()
    binding = _command_setup(engine)
    with Session(engine) as db:
        reply = run_command(db, binding, "wechat_p2p_u1", parse_command("/切换 财务"))
        db.commit()
        assert reply == "已切换到「财务」，后续消息由 TA 回复。上下文各自独立，输入 /员工 查看列表。"
        current, _ = resolve_current_agent(db, binding, "wechat_p2p_u1")
        assert current == "agent_cw"

        reply = run_command(db, binding, "wechat_p2p_u1", parse_command("/当前"))
        assert "「财务」" in reply

        again = run_command(db, binding, "wechat_p2p_u1", parse_command("/财务"))
        assert again == "当前已经是「财务」。"


def test_run_command_switch_unknown_and_empty() -> None:
    engine = _test_engine()
    binding = _command_setup(engine)
    with Session(engine) as db:
        reply = run_command(db, binding, "wechat_p2p_u1", parse_command("/切换 保安"))
        assert "没有找到员工「保安」" in reply
        reply = run_command(db, binding, "wechat_p2p_u1", parse_command("/切换"))
        assert reply.startswith("用法：/切换")


def test_run_command_help() -> None:
    engine = _test_engine()
    binding = _command_setup(engine)
    with Session(engine) as db:
        assert run_command(db, binding, "wechat_p2p_u1", parse_command("/帮助")) == HELP_TEXT


# ---------- intake 集成 ----------


def _notices(engine) -> list[ChannelDelivery]:
    with Session(engine) as db:
        return db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "notice")
        ).all()


def test_command_message_creates_no_session_and_notices_via_outbox() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, mounts=[("agent_xz", "行政", True), ("agent_cw", "财务", False)])
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_cmd1", "/员工"), db_engine=engine) is False
    assert RecordingAgentLoop.calls == []

    notices = _notices(engine)
    assert len(notices) == 1
    assert notices[0].session_id == "conv:wechat_p2p_user_ab12cd34@im.wechat"
    assert "可调度员工" in notices[0].text
    assert notices[0].status == "pending"

    with Session(engine) as db:
        assert db.exec(select(ChatSession)).all() == []
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "done"


def test_switch_then_next_message_routes_to_new_agent_session() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, mounts=[("agent_xz", "行政", True), ("agent_cw", "财务", False)])
    binding = _load_binding(engine, binding_id)

    # 默认员工会话
    assert process_inbound(binding, _p2p_message("evt_1", "帮我订会议室"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"
    session_xz = RecordingAgentLoop.calls[-1].session_id

    # 切换到财务
    assert process_inbound(binding, _p2p_message("evt_2", "/切换 财务"), db_engine=engine) is False
    assert process_inbound(binding, _p2p_message("evt_3", "报销怎么走"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_cw"
    session_cw = RecordingAgentLoop.calls[-1].session_id
    assert session_cw != session_xz

    # 切回行政:原会话还在(上下文独立保留)
    assert process_inbound(binding, _p2p_message("evt_4", "/切换 行政"), db_engine=engine) is False
    assert process_inbound(binding, _p2p_message("evt_5", "会议室订好了吗"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"
    assert RecordingAgentLoop.calls[-1].session_id == session_xz

    with Session(engine) as db:
        sessions = db.exec(select(ChatSession)).all()
        assert len(sessions) == 2
        by_agent = {s.agent_id: s for s in sessions}
        assert by_agent["agent_xz"].channel_binding_id == binding_id
        assert by_agent["agent_cw"].channel_binding_id == binding_id
    # 两次切换各产生一条 notice
    assert len(_notices(engine)) == 2


def test_group_pointer_independent_from_p2p() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, mounts=[("agent_xz", "行政", True), ("agent_cw", "财务", False)])
    binding = _load_binding(engine, binding_id)

    # 群里切到财务
    assert process_inbound(binding, _group_message("evt_g1", "/切换 财务"), db_engine=engine) is False
    assert process_inbound(binding, _group_message("evt_g2", "群里问报销"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_cw"

    # 私聊仍是默认行政
    assert process_inbound(binding, _p2p_message("evt_p1", "私聊问行政"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"

    # 另一个群独立指针
    assert process_inbound(binding, _group_message("evt_g3", "新群第一句", group_id="room_999"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"


def test_legacy_binding_routes_to_binding_default_agent() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)  # 无挂载行(存量 v1 绑定)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_l1", "你好"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"


def test_pointer_reset_notice_when_agent_unmounted() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, mounts=[("agent_xz", "行政", True), ("agent_cw", "财务", False)])
    binding = _load_binding(engine, binding_id)

    # 切到财务后,把财务从挂载集移除
    assert process_inbound(binding, _p2p_message("evt_r1", "/切换 财务"), db_engine=engine) is False
    with Session(engine) as db:
        row = db.exec(
            select(ChannelBindingAgent).where(
                ChannelBindingAgent.binding_id == binding_id,
                ChannelBindingAgent.agent_id == "agent_cw",
            )
        ).one()
        db.delete(row)
        db.commit()

    assert process_inbound(binding, _p2p_message("evt_r2", "还在吗"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"
    notices = _notices(engine)
    assert any("已下线" in n.text and "「行政」" in n.text for n in notices)


def test_agent_names_lookup() -> None:
    engine = _test_engine()
    _seed_binding(engine)
    with Session(engine) as db:
        names = agent_names(db, "tenant_demo", ["agent_xz", "agent_cw", "agent_missing"])
        assert names == {"agent_xz": "行政", "agent_cw": "财务"}


# ---------- staging 直查优先 ----------


def test_staging_prefers_channel_binding_id() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        # 绑定默认员工是行政,但会话属于财务(账号化路由后的状态)
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            status="active",
            created_by_user_id="user_owner",
        )
        db.add(binding)
        db.commit()
        chat_session = ChatSession(
            id="session_routed",
            tenant_id="tenant_demo",
            agent_id="agent_cw",
            channel="wechat",
            external_conv_id="wechat_p2p_u1",
            channel_target_json={"to_user_id": "u1", "context_token": "ctx"},
            channel_binding_id=binding.id,
        )
        message = Message(
            id="msg_routed",
            tenant_id="tenant_demo",
            session_id="session_routed",
            role="assistant",
            content="财务回复",
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()
        deliveries = db.exec(select(ChannelDelivery)).all()
        assert len(deliveries) == 1
        assert deliveries[0].binding_id == binding.id


def test_staging_fallback_without_channel_binding_id() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            status="active",
            created_by_user_id="user_owner",
        )
        db.add(binding)
        db.commit()
        chat_session = ChatSession(
            id="session_legacy",
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            external_conv_id="wechat_p2p_u1",
            channel_target_json={"to_user_id": "u1", "context_token": "ctx"},
        )
        message = Message(
            id="msg_legacy",
            tenant_id="tenant_demo",
            session_id="session_legacy",
            role="assistant",
            content="回复",
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()
        assert len(db.exec(select(ChannelDelivery)).all()) == 1


def test_staging_skips_when_binding_id_points_to_disabled() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        binding = ChannelBinding(
            tenant_id="tenant_demo", agent_id="agent_xz", channel="wechat", status="disabled"
        )
        db.add(binding)
        db.commit()
        chat_session = ChatSession(
            id="session_disabled",
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            channel_target_json={"to_user_id": "u1", "context_token": "ctx"},
            channel_binding_id=binding.id,
        )
        message = Message(
            id="msg_disabled",
            tenant_id="tenant_demo",
            session_id="session_disabled",
            role="assistant",
            content="回复",
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()
        assert db.exec(select(ChannelDelivery)).all() == []


# ---------- 迁移回填 ----------


def test_binding_agents_backfill_is_idempotent(monkeypatch, tmp_path) -> None:
    from sqlalchemy import text as sa_text

    from app.db import database

    db_path = tmp_path / "backfill.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa_text(
                "INSERT INTO channel_bindings (id, tenant_id, agent_id, channel, status, connected, created_at, updated_at) "
                "VALUES ('chan_legacy', 'tenant_demo', 'agent_xz', 'wechat', 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    monkeypatch.setattr(database, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_sqlite_skill_schema()
    with engine.begin() as conn:
        rows = conn.execute(sa_text("SELECT * FROM channel_binding_agents")).mappings().all()
        assert len(rows) == 1
        assert rows[0]["binding_id"] == "chan_legacy"
        assert rows[0]["agent_id"] == "agent_xz"
        assert rows[0]["is_default"] == 1

    # 重复执行:迁移记录存在,不再插入
    with engine.begin() as conn:
        conn.execute(sa_text("DELETE FROM channel_binding_agents"))
    database._migrate_sqlite_skill_schema()
    with engine.begin() as conn:
        rows = conn.execute(sa_text("SELECT * FROM channel_binding_agents")).mappings().all()
        assert rows == []
        applied = conn.execute(
            sa_text("SELECT id FROM app_data_migrations WHERE id = :id"),
            {"id": database._CHANNEL_BINDING_AGENTS_BACKFILL_MIGRATION_ID},
        ).first()
        assert applied is not None


# ---------- API:POST 自动挂载 / GET agents / PUT ----------


def _make_api_client(engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.channels as channels_api
    from app.db import get_session

    app = FastAPI()
    app.include_router(channels_api.router)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return TestClient(app)


def _seed_api_users(engine) -> dict[str, User]:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        owner = User(id="user_owner", tenant_id="tenant_demo", username="owner", password_hash="x")
        other = User(id="user_other", tenant_id="tenant_demo", username="other", password_hash="x")
        db.add(owner)
        db.add(other)
        for agent_id, name in (("agent_xz", "行政"), ("agent_cw", "财务"), ("agent_rs", "人事")):
            db.add(
                AgentProfile(
                    id=agent_id,
                    tenant_id="tenant_demo",
                    name=name,
                    metadata_json={"owner_user_id": owner.id},
                )
            )
        db.commit()
        for user in (owner, other):
            db.refresh(user)
            db.expunge(user)
        return {"owner": owner, "other": other}


def _auth(user: User) -> dict[str, str]:
    from app.security.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user)}"}


def test_post_binding_auto_mounts_default_agent() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    client = _make_api_client(engine)

    created = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "agent_id": "agent_xz", "channel": "wechat"},
        headers=_auth(users["owner"]),
    )
    assert created.status_code == 200
    binding_id = created.json()["id"]
    agents = created.json()["agents"]
    assert [(a["agent_id"], a["is_default"]) for a in agents] == [("agent_xz", True)]
    assert agents[0]["name"] == "行政"

    listed = client.get(
        f"/api/enterprise/channels/{binding_id}/agents?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert listed.status_code == 200
    assert [(a["agent_id"], a["name"], a["is_default"]) for a in listed.json()] == [
        ("agent_xz", "行政", True)
    ]


def test_get_agents_legacy_fallback() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    with Session(engine) as db:
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            status="active",
            created_by_user_id="user_owner",
        )
        db.add(binding)
        db.commit()
        binding_id = binding.id

    client = _make_api_client(engine)
    listed = client.get(
        f"/api/enterprise/channels/{binding_id}/agents?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert listed.status_code == 200
    assert [(a["agent_id"], a["is_default"]) for a in listed.json()] == [("agent_xz", True)]


def test_put_agents_replaces_mounts_and_normalizes_default() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    with Session(engine) as db:
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            status="active",
            created_by_user_id="user_owner",
        )
        db.add(binding)
        db.commit()
        binding_id = binding.id

    client = _make_api_client(engine)
    # 未标默认 → 取第一个
    updated = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_cw"}, {"agent_id": "agent_xz"}]},
        headers=_auth(users["owner"]),
    )
    assert updated.status_code == 200
    payload = updated.json()
    assert [(a["agent_id"], a["is_default"]) for a in payload["agents"]] == [
        ("agent_cw", True),
        ("agent_xz", False),
    ]
    assert payload["agent_id"] == "agent_cw"

    # 多标默认 → 取首个标记
    updated = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={
            "agents": [
                {"agent_id": "agent_xz", "is_default": True},
                {"agent_id": "agent_cw", "is_default": True},
            ]
        },
        headers=_auth(users["owner"]),
    )
    assert [(a["agent_id"], a["is_default"]) for a in updated.json()["agents"]] == [
        ("agent_xz", True),
        ("agent_cw", False),
    ]

    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.agent_id == "agent_xz"
        mounts = db.exec(
            select(ChannelBindingAgent).where(ChannelBindingAgent.binding_id == binding_id)
        ).all()
        assert len(mounts) == 2


def test_put_agents_validations() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    with Session(engine) as db:
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            status="active",
            created_by_user_id="user_owner",
        )
        db.add(binding)
        db.commit()
        binding_id = binding.id

    client = _make_api_client(engine)
    # 非 manager 403
    forbidden = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_xz"}]},
        headers=_auth(users["other"]),
    )
    assert forbidden.status_code == 403

    # 空列表 400
    empty = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": []},
        headers=_auth(users["owner"]),
    )
    assert empty.status_code == 400

    # 未知 agent 404
    unknown = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_xz"}, {"agent_id": "agent_nope"}]},
        headers=_auth(users["owner"]),
    )
    assert unknown.status_code == 404

    # 重复 agent 400
    duplicated = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_xz"}, {"agent_id": "agent_xz"}]},
        headers=_auth(users["owner"]),
    )
    assert duplicated.status_code == 400
