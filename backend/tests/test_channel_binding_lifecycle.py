from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.service_outbox import stage_channel_delivery
from app.channels.service_session import adopt_orphan_channel_sessions
from app.db.models import (
    AgentProfile,
    ChannelBinding,
    ChannelBindingAgent,
    ChannelConvState,
    ChannelDelivery,
    ChatSession,
    Message,
    Tenant,
    User,
)


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


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


def _seed_users(engine) -> dict[str, User]:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        owner = User(id="user_owner", tenant_id="tenant_demo", username="owner", password_hash="x")
        db.add(owner)
        db.add(
            AgentProfile(
                id="agent_xz",
                tenant_id="tenant_demo",
                name="行政",
                metadata_json={"owner_user_id": owner.id},
            )
        )
        db.add(
            AgentProfile(
                id="agent_cw",
                tenant_id="tenant_demo",
                name="财务",
                metadata_json={"owner_user_id": owner.id},
            )
        )
        db.commit()
        db.refresh(owner)
        db.expunge(owner)
        return {"owner": owner}


def _auth(user: User) -> dict[str, str]:
    from app.security.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user)}"}


# ---------- 级联删除 ----------


def test_delete_binding_cascades_mounts_and_conv_states(monkeypatch) -> None:
    import app.api.channels as channels_api

    engine = _test_engine()
    users = _seed_users(engine)
    with Session(engine) as db:
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            status="active",
            created_by_user_id="user_owner",
        )
        db.add(binding)
        db.flush()
        db.add(
            ChannelBindingAgent(
                tenant_id="tenant_demo", binding_id=binding.id, agent_id="agent_xz", is_default=True
            )
        )
        db.add(
            ChannelBindingAgent(
                tenant_id="tenant_demo", binding_id=binding.id, agent_id="agent_cw"
            )
        )
        db.add(
            ChannelConvState(
                tenant_id="tenant_demo",
                binding_id=binding.id,
                external_conv_id="wechat_p2p_u1",
                current_agent_id="agent_cw",
            )
        )
        db.commit()
        binding_id = binding.id

    client = _make_api_client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)
    response = client.delete(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 204

    with Session(engine) as db:
        assert db.get(ChannelBinding, binding_id) is None
        assert db.exec(select(ChannelBindingAgent)).all() == []
        assert db.exec(select(ChannelConvState)).all() == []


# ---------- 重绑自愈(孤儿会话认领) ----------


def test_adopt_orphan_channel_sessions() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        dead_binding_id = "chan_dead"
        alive_other = ChannelBinding(
            id="chan_other_alive",
            tenant_id="tenant_demo",
            agent_id="agent_cw",
            channel="wechat",
            status="active",
        )
        new_binding = ChannelBinding(
            tenant_id="tenant_demo", agent_id="agent_xz", channel="wechat", status="pending"
        )
        db.add(alive_other)
        db.add(new_binding)
        db.flush()
        db.add(
            ChannelBindingAgent(
                tenant_id="tenant_demo",
                binding_id=new_binding.id,
                agent_id="agent_xz",
                is_default=True,
            )
        )
        db.add(
            ChannelBindingAgent(
                tenant_id="tenant_demo", binding_id=new_binding.id, agent_id="agent_cw"
            )
        )
        sessions = [
            # 指向已删除绑定 → 认领
            ChatSession(
                id="s_dead",
                tenant_id="tenant_demo",
                agent_id="agent_xz",
                channel="wechat",
                external_conv_id="wechat_p2p_u1",
                channel_binding_id=dead_binding_id,
            ),
            # 空 channel_binding_id 且 agent 在挂载集 → 认领
            ChatSession(
                id="s_null",
                tenant_id="tenant_demo",
                agent_id="agent_cw",
                channel="wechat",
                external_conv_id="wechat_p2p_u2",
            ),
            # 他渠道 → 不动
            ChatSession(
                id="s_other_channel",
                tenant_id="tenant_demo",
                agent_id="agent_xz",
                channel="feishu",
                external_conv_id="feishu_p2p_u1",
                channel_binding_id=dead_binding_id,
            ),
            # 挂载集外 agent → 不动
            ChatSession(
                id="s_outside_mounts",
                tenant_id="tenant_demo",
                agent_id="agent_other",
                channel="wechat",
                external_conv_id="wechat_p2p_u3",
                channel_binding_id=dead_binding_id,
            ),
            # 已挂在现存绑定上 → 不动
            ChatSession(
                id="s_alive",
                tenant_id="tenant_demo",
                agent_id="agent_xz",
                channel="wechat",
                external_conv_id="wechat_p2p_u4",
                channel_binding_id="chan_other_alive",
            ),
        ]
        for row in sessions:
            db.add(row)
        db.commit()

        adopted = adopt_orphan_channel_sessions(db, new_binding)
        db.commit()
        assert adopted == 2
        assert db.get(ChatSession, "s_dead").channel_binding_id == new_binding.id
        assert db.get(ChatSession, "s_null").channel_binding_id == new_binding.id
        assert db.get(ChatSession, "s_other_channel").channel_binding_id == dead_binding_id
        assert db.get(ChatSession, "s_outside_mounts").channel_binding_id == dead_binding_id
        assert db.get(ChatSession, "s_alive").channel_binding_id == "chan_other_alive"


def test_adopt_orphan_sessions_legacy_mount_fallback() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        # 无挂载行(存量绑定):挂载集回退为 [binding.agent_id]
        binding = ChannelBinding(
            tenant_id="tenant_demo", agent_id="agent_xz", channel="wechat", status="pending"
        )
        db.add(binding)
        db.commit()
        db.add(
            ChatSession(
                id="s_legacy",
                tenant_id="tenant_demo",
                agent_id="agent_xz",
                channel="wechat",
                external_conv_id="wechat_p2p_u1",
                channel_binding_id="chan_dead",
            )
        )
        db.commit()

        assert adopt_orphan_channel_sessions(db, binding) == 1
        assert db.get(ChatSession, "s_legacy").channel_binding_id == binding.id


# ---------- 投递回退挂载感知 ----------


def _stageable_session(**overrides) -> ChatSession:
    values = {
        "id": "session_x",
        "tenant_id": "tenant_demo",
        "agent_id": "agent_cw",
        "channel": "wechat",
        "channel_target_json": {"to_user_id": "u1", "context_token": "ctx"},
    }
    values.update(overrides)
    return ChatSession(**values)


def _message(session_id: str) -> Message:
    return Message(
        id=f"msg_{session_id}",
        tenant_id="tenant_demo",
        session_id=session_id,
        role="assistant",
        content="回复",
    )


def test_delivery_fallback_finds_active_binding_via_mounts() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        # 会话直挂的绑定已删除;新 active 绑定挂载了 agent_cw
        alive = ChannelBinding(
            tenant_id="tenant_demo", agent_id="agent_xz", channel="wechat", status="active"
        )
        db.add(alive)
        db.flush()
        db.add(
            ChannelBindingAgent(
                tenant_id="tenant_demo", binding_id=alive.id, agent_id="agent_cw"
            )
        )
        db.commit()
        chat_session = _stageable_session(channel_binding_id="chan_dead")
        message = _message(chat_session.id)
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()
        deliveries = db.exec(select(ChannelDelivery)).all()
        assert len(deliveries) == 1
        assert deliveries[0].binding_id == alive.id


def test_delivery_fallback_default_agent_still_works() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        # 无挂载行:挂载集回退 binding.agent_id(与旧 (agent_id, channel) 反查等价)
        binding = ChannelBinding(
            tenant_id="tenant_demo", agent_id="agent_xz", channel="wechat", status="active"
        )
        db.add(binding)
        db.commit()
        chat_session = _stageable_session(agent_id="agent_xz", channel_binding_id=None)
        message = _message(chat_session.id)
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()
        deliveries = db.exec(select(ChannelDelivery)).all()
        assert len(deliveries) == 1
        assert deliveries[0].binding_id == binding.id


def test_delivery_gives_up_when_no_active_binding_matches() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        # 唯一绑定已停用,且无其他 active 绑定
        binding = ChannelBinding(
            tenant_id="tenant_demo", agent_id="agent_xz", channel="wechat", status="disabled"
        )
        db.add(binding)
        db.commit()
        chat_session = _stageable_session(channel_binding_id=binding.id)
        message = _message(chat_session.id)
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()
        assert db.exec(select(ChannelDelivery)).all() == []
