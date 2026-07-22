import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.channels.service_intake as intake_module
import app.core.agent_loop as agent_loop_module
from app.channels.adapters.wecom import normalize_wecom_frame
from app.channels.service_identity import (
    channel_username,
    external_account_scope,
    resolve_or_provision_user,
)
from app.channels.service_intake import process_inbound
from app.channels.service_session import adopt_orphan_channel_sessions
from app.db import database
from app.db.models import (
    AgentProfile,
    ChannelBinding,
    ChannelIdentity,
    ChannelInboundEvent,
    ChatSession,
    MemoryRecord,
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


def _seed_wecom_binding(
    engine,
    *,
    tenant_id: str = "tenant_demo",
    agent_id: str = "agent_1",
    corp_id: str | None = None,
    bot_id: str = "aib_bot1",
) -> str:
    config = {"bot_id": bot_id}
    if corp_id:
        config["corp_id"] = corp_id
    with Session(engine) as db:
        if not db.get(Tenant, tenant_id):
            db.add(Tenant(id=tenant_id, name=tenant_id))
        if not db.get(AgentProfile, agent_id):
            db.add(AgentProfile(id=agent_id, tenant_id=tenant_id, name=agent_id, metadata_json={}))
        binding = ChannelBinding(
            tenant_id=tenant_id,
            agent_id=agent_id,
            channel="wecom",
            status="active",
            config_json=config,
            created_by_user_id="user_owner",
        )
        db.add(binding)
        db.commit()
        return binding.id


def _load_binding(engine, binding_id: str) -> ChannelBinding:
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        db.expunge(binding)
        return binding


def _wecom_inbound(event_id: str, text: str, *, userid: str = "zhangsan", group: bool = False):
    body = {
        "msgid": event_id,
        "aibotid": "aib_bot1",
        "chattype": "group" if group else "single",
        "from": {"userid": userid},
        "msgtype": "text",
        "text": {"content": text},
    }
    if group:
        body["chatid"] = "wr_room1"
    return normalize_wecom_frame(
        {"cmd": "aibot_msg_callback", "headers": {"req_id": f"req_{event_id}"}, "body": body}
    )


class RecordingAgentLoop:
    calls: list = []

    def __init__(self, db):
        self.db = db

    def handle_turn(self, request):
        type(self).calls.append(request)
        self.db.commit()


@pytest.fixture(autouse=True)
def _fake_agent_loop(monkeypatch):
    RecordingAgentLoop.calls = []
    monkeypatch.setattr(agent_loop_module, "AgentLoop", RecordingAgentLoop)
    monkeypatch.setattr(intake_module, "_send_wechat_typing", lambda *args, **kwargs: None)
    yield


def _users_of(engine, tenant_id: str, channel: str, external_id: str, scope: str) -> User:
    with Session(engine) as db:
        return resolve_or_provision_user(db, tenant_id, channel, external_id, None, scope)


# ---------- scope helper ----------


def test_external_account_scope_resolution() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        wechat = ChannelBinding(tenant_id="t", agent_id="a", channel="wechat", config_json={})
        corp = ChannelBinding(
            tenant_id="t",
            agent_id="a2",
            channel="wecom",
            config_json={"bot_id": "b1", "corp_id": "corpX"},
        )
        bot_only = ChannelBinding(
            tenant_id="t", agent_id="a3", channel="wecom", config_json={"bot_id": "b2"}
        )
        empty = ChannelBinding(
            id="chan_fallback", tenant_id="t", agent_id="a4", channel="wecom", config_json={}
        )
        assert external_account_scope(db, wechat) == ""
        assert external_account_scope(db, corp) == "corpX"
        assert external_account_scope(db, bot_only) == "b2"
        assert external_account_scope(db, empty) == "chan_fallback"


# ---------- 维护者场景 ----------


def test_same_userid_different_tenants_get_different_users() -> None:
    engine = _test_engine()
    binding_a = _seed_wecom_binding(engine, tenant_id="tenant_a", corp_id="corpA")
    binding_b = _seed_wecom_binding(engine, tenant_id="tenant_b", corp_id="corpA", agent_id="agent_2")

    binding = _load_binding(engine, binding_a)
    assert process_inbound(binding, _wecom_inbound("m1", "你好"), db_engine=engine) is True
    user_a = RecordingAgentLoop.calls[-1].user_id

    binding = _load_binding(engine, binding_b)
    assert process_inbound(binding, _wecom_inbound("m1", "你好"), db_engine=engine) is True
    user_b = RecordingAgentLoop.calls[-1].user_id

    assert user_a != user_b


def test_same_tenant_different_corp_same_userid_get_different_users() -> None:
    engine = _test_engine()
    binding_a = _seed_wecom_binding(engine, corp_id="corpA", bot_id="bot_a")
    binding_b = _seed_wecom_binding(engine, corp_id="corpB", bot_id="bot_b", agent_id="agent_2")

    binding = _load_binding(engine, binding_a)
    assert process_inbound(binding, _wecom_inbound("m1", "你好"), db_engine=engine) is True
    binding = _load_binding(engine, binding_b)
    assert process_inbound(binding, _wecom_inbound("m2", "你好"), db_engine=engine) is True

    user_a, user_b = RecordingAgentLoop.calls[0].user_id, RecordingAgentLoop.calls[1].user_id
    assert user_a != user_b
    with Session(engine) as db:
        usernames = {row.username for row in db.exec(select(User)).all()}
        # 同租户两企业同名不撞 username
        assert channel_username("tenant_demo", "wecom", "zhangsan", "corpA") in usernames
        assert channel_username("tenant_demo", "wecom", "zhangsan", "corpB") in usernames
        identities = db.exec(select(ChannelIdentity)).all()
        assert len(identities) == 2
        assert {i.external_account_scope for i in identities} == {"corpA", "corpB"}


def test_same_corp_different_bindings_share_identity() -> None:
    engine = _test_engine()
    binding_1 = _seed_wecom_binding(engine, corp_id="corpX", bot_id="bot_1")
    binding_2 = _seed_wecom_binding(engine, corp_id="corpX", bot_id="bot_2", agent_id="agent_2")

    binding = _load_binding(engine, binding_1)
    assert process_inbound(binding, _wecom_inbound("m1", "你好"), db_engine=engine) is True
    binding = _load_binding(engine, binding_2)
    assert process_inbound(binding, _wecom_inbound("m2", "你好"), db_engine=engine) is True

    assert RecordingAgentLoop.calls[0].user_id == RecordingAgentLoop.calls[1].user_id
    with Session(engine) as db:
        assert len(db.exec(select(ChannelIdentity)).all()) == 1


def test_cross_corp_same_msgid_events_both_processed() -> None:
    engine = _test_engine()
    binding_a = _seed_wecom_binding(engine, corp_id="corpA", bot_id="bot_a")
    binding_b = _seed_wecom_binding(engine, corp_id="corpB", bot_id="bot_b", agent_id="agent_2")

    binding = _load_binding(engine, binding_a)
    assert process_inbound(binding, _wecom_inbound("same_msgid", "甲企业消息"), db_engine=engine) is True
    binding = _load_binding(engine, binding_b)
    # 同一 msgid 在另一企业(另一绑定)不再被幂等误杀
    assert process_inbound(binding, _wecom_inbound("same_msgid", "乙企业消息"), db_engine=engine) is True

    assert len(RecordingAgentLoop.calls) == 2
    with Session(engine) as db:
        events = db.exec(select(ChannelInboundEvent)).all()
        assert len(events) == 2
        assert {event.binding_id for event in events} == {binding_a, binding_b}


def test_bind_unbind_never_migrates_other_corp_data() -> None:
    engine = _test_engine()
    binding_a = _seed_wecom_binding(engine, corp_id="corpA", bot_id="bot_a")
    _seed_wecom_binding(engine, corp_id="corpB", bot_id="bot_b", agent_id="agent_2")
    with Session(engine) as db:
        db.add(
            User(id="user_web", tenant_id="tenant_demo", username="webadmin", display_name="管理员", password_hash="x")
        )
        lazy_a = User(
            id="lazy_a",
            tenant_id="tenant_demo",
            username=channel_username("tenant_demo", "wecom", "zhangsan", "corpA"),
            display_name="企微用户 zhangsan",
            source="wecom",
            password_hash="x",
        )
        lazy_b = User(
            id="lazy_b",
            tenant_id="tenant_demo",
            username=channel_username("tenant_demo", "wecom", "zhangsan", "corpB"),
            display_name="企微用户 zhangsan",
            source="wecom",
            password_hash="x",
        )
        db.add(lazy_a)
        db.add(lazy_b)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="corpA",
                external_user_id="zhangsan",
                staffdeck_user_id=lazy_a.id,
            )
        )
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="corpB",
                external_user_id="zhangsan",
                staffdeck_user_id=lazy_b.id,
            )
        )
        db.add(
            ChatSession(
                id="s_a",
                tenant_id="tenant_demo",
                user_id="lazy_a",
                agent_id="agent_1",
                channel="wecom",
                external_conv_id="wecom_corpA_p2p_zhangsan",
            )
        )
        db.add(
            ChatSession(
                id="s_b",
                tenant_id="tenant_demo",
                user_id="lazy_b",
                agent_id="agent_1",
                channel="wecom",
                external_conv_id="wecom_corpB_p2p_zhangsan",
            )
        )
        db.add(
            MemoryRecord(
                id="mem_a",
                tenant_id="tenant_demo",
                user_id="lazy_a",
                username=lazy_a.username,
                session_id="s_a",
                content="A 企业偏好",
            )
        )
        db.add(
            MemoryRecord(
                id="mem_b",
                tenant_id="tenant_demo",
                user_id="lazy_b",
                username=lazy_b.username,
                session_id="s_b",
                content="B 企业偏好",
            )
        )
        from datetime import timedelta

        from app.db.models import ChannelBindCode, utc_now

        db.add(
            ChannelBindCode(
                tenant_id="tenant_demo",
                user_id="user_web",
                code="123456",
                expires_at=utc_now() + timedelta(minutes=10),
            )
        )
        db.commit()

    binding = _load_binding(engine, binding_a)
    # 绑定 corpA 身份到 web 账号:只迁 corpA 的会话与记忆
    assert process_inbound(binding, _wecom_inbound("m_bind", "/绑定 123456"), db_engine=engine) is False
    with Session(engine) as db:
        assert db.get(ChatSession, "s_a").user_id == "user_web"
        assert db.get(MemoryRecord, "mem_a").user_id == "user_web"
        # 他企业(corpB)的会话与记忆不动
        assert db.get(ChatSession, "s_b").user_id == "lazy_b"
        assert db.get(MemoryRecord, "mem_b").user_id == "lazy_b"

    # 解绑:corpA 迁回懒建账号,corpB 仍然不动
    assert process_inbound(binding, _wecom_inbound("m_unbind", "/解绑"), db_engine=engine) is False
    with Session(engine) as db:
        assert db.get(ChatSession, "s_a").user_id == "lazy_a"
        assert db.get(MemoryRecord, "mem_a").user_id == "lazy_a"
        assert db.get(ChatSession, "s_b").user_id == "lazy_b"
        assert db.get(MemoryRecord, "mem_b").user_id == "lazy_b"


# ---------- 认领不认领他 scope 会话 ----------


def test_adopt_skips_other_scope_sessions() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine, corp_id="corpA", bot_id="bot_a")
    with Session(engine) as db:
        for session_id, conv in (
            ("s_same", "wecom_corpA_p2p_zhangsan"),
            ("s_other", "wecom_corpB_p2p_zhangsan"),
            ("s_legacy", "wecom_legacy_p2p_lisi"),
        ):
            db.add(
                ChatSession(
                    id=session_id,
                    tenant_id="tenant_demo",
                    agent_id="agent_1",
                    channel="wecom",
                    external_conv_id=conv,
                    channel_binding_id="chan_dead",
                )
            )
        db.commit()
        binding = db.get(ChannelBinding, binding_id)
        assert adopt_orphan_channel_sessions(db, binding) == 1
        assert db.get(ChatSession, "s_same").channel_binding_id == binding_id
        assert db.get(ChatSession, "s_other").channel_binding_id == "chan_dead"
        assert db.get(ChatSession, "s_legacy").channel_binding_id == "chan_dead"


# ---------- 迁移 ----------


def _build_legacy_scope_schema(engine) -> None:
    """手工搭出 scope 重构前的老表结构。"""
    with engine.begin() as conn:
        conn.execute(
            sa_text(
                """
                CREATE TABLE channel_identities (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR,
                    channel VARCHAR,
                    external_user_id VARCHAR NOT NULL,
                    staffdeck_user_id VARCHAR,
                    display_name VARCHAR,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_channel_identity_external UNIQUE (channel, external_user_id)
                )
                """
            )
        )
        conn.execute(
            sa_text(
                """
                CREATE TABLE channel_inbound_events (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR,
                    binding_id VARCHAR,
                    channel VARCHAR,
                    event_id VARCHAR NOT NULL,
                    payload_json JSON,
                    status VARCHAR,
                    error VARCHAR,
                    processed_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_channel_inbound_event UNIQUE (channel, event_id)
                )
                """
            )
        )
        conn.execute(
            sa_text(
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
        conn.execute(
            sa_text(
                """
                CREATE TABLE sessions (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR,
                    user_id VARCHAR,
                    agent_id VARCHAR,
                    channel VARCHAR,
                    external_conv_id VARCHAR,
                    channel_binding_id VARCHAR,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            sa_text(
                "INSERT INTO channel_bindings (id, tenant_id, agent_id, channel, status, config_json) "
                "VALUES ('chan_corpA', 'tenant_demo', 'agent_1', 'wecom', 'active', '{\"bot_id\": \"aib_bot1\", \"corp_id\": \"corpA\"}')"
            )
        )
        conn.execute(
            sa_text(
                "INSERT INTO channel_identities (id, tenant_id, channel, external_user_id, staffdeck_user_id) VALUES "
                "('ci_1', 'tenant_demo', 'wecom', 'zhangsan', 'lazy_1'), "
                "('ci_2', 'tenant_demo', 'wechat', 'wxid_1', 'lazy_2'), "
                "('ci_3', 'tenant_b', 'wecom', 'lisi', 'lazy_3')"
            )
        )
        conn.execute(
            sa_text(
                "INSERT INTO channel_inbound_events (id, tenant_id, binding_id, channel, event_id, status) VALUES "
                "('ev_1', 'tenant_demo', 'chan_corpA', 'wecom', 'm1', 'done')"
            )
        )
        conn.execute(
            sa_text(
                "INSERT INTO sessions (id, tenant_id, channel, external_conv_id, channel_binding_id) VALUES "
                "('s_1', 'tenant_demo', 'wecom', 'wecom_p2p_zhangsan', 'chan_corpA'), "
                "('s_2', 'tenant_demo', 'wecom', 'wecom_p2p_lisi', 'chan_dead'), "
                "('s_3', 'tenant_demo', 'wecom', 'wecom_group_wr_room1', 'chan_corpA')"
            )
        )


def test_channel_scope_rebuild_migration(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "scope.db"
    engine = create_engine(f"sqlite:///{db_path}")
    _build_legacy_scope_schema(engine)
    monkeypatch.setattr(database, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_sqlite_skill_schema()
    with engine.begin() as conn:
        identities = conn.execute(
            sa_text(
                "SELECT id, external_account_scope FROM channel_identities ORDER BY id"
            )
        ).all()
        assert dict(identities) == {"ci_1": "corpA", "ci_2": "", "ci_3": "legacy"}
        convs = dict(
            conn.execute(sa_text("SELECT id, external_conv_id FROM sessions")).all()
        )
        assert convs == {
            "s_1": "wecom_corpA_p2p_zhangsan",
            "s_2": "wecom_legacy_p2p_lisi",
            "s_3": "wecom_corpA_group_wr_room1",
        }
        # 新唯一约束生效:同 binding 同 event 冲突、跨 binding 同 event 放行
        conn.execute(
            sa_text(
                "INSERT INTO channel_inbound_events (id, tenant_id, binding_id, channel, event_id, status) "
                "VALUES ('ev_2', 'tenant_demo', 'chan_other', 'wecom', 'm1', 'done')"
            )
        )
        with pytest.raises(Exception):
            conn.execute(
                sa_text(
                    "INSERT INTO channel_inbound_events (id, tenant_id, binding_id, channel, event_id, status) "
                    "VALUES ('ev_3', 'tenant_demo', 'chan_corpA', 'wecom', 'm1', 'done')"
                )
            )

    # 幂等:重跑不炸、数据不变
    database._migrate_sqlite_skill_schema()
    with engine.begin() as conn:
        assert conn.execute(sa_text("SELECT COUNT(*) FROM channel_identities")).scalar_one() == 3
        applied = conn.execute(
            sa_text("SELECT id FROM app_data_migrations WHERE id = :id"),
            {"id": database._CHANNEL_SCOPE_REBUILD_MIGRATION_ID},
        ).first()
        assert applied is not None


# ---------- API:corp_id 与 meta ----------


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


def test_wecom_credentials_accepts_corp_id_and_meta_lists_it(monkeypatch) -> None:
    import app.api.channels as channels_api

    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        owner = User(id="user_owner", tenant_id="tenant_demo", username="owner", password_hash="x")
        db.add(owner)
        db.add(
            AgentProfile(
                id="agent_1",
                tenant_id="tenant_demo",
                name="客服",
                metadata_json={"owner_user_id": owner.id},
            )
        )
        db.commit()
        db.refresh(owner)
        db.expunge(owner)
    binding_id = _seed_wecom_binding(engine)
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.status = "pending"
        binding.credentials_enc = None
        db.add(binding)
        db.commit()

    from app.security.auth import create_access_token

    client = _make_api_client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)
    headers = {"Authorization": f"Bearer {create_access_token(owner)}"}

    response = client.post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={
            "tenant_id": "tenant_demo",
            "bot_id": "aib_bot1",
            "secret": "bot_secret",
            "corp_id": "corpA",
        },
        headers=headers,
    )
    assert response.status_code == 200
    with Session(engine) as db:
        config = db.get(ChannelBinding, binding_id).config_json
        assert config["corp_id"] == "corpA"

    meta = client.get("/api/enterprise/channels/meta?tenant_id=tenant_demo", headers=headers)
    wecom = next(item for item in meta.json() if item["channel"] == "wecom")
    fields = {field["key"]: field for field in wecom["credential_fields"]}
    assert fields["corp_id"]["optional"] is True
    assert fields["corp_id"]["secret"] is False


# ---------- scope 变化连续性迁移 ----------


def _seed_owner(engine):
    from app.security.auth import create_access_token

    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        owner = User(id="user_owner", tenant_id="tenant_demo", username="owner", password_hash="x")
        db.add(owner)
        db.add(
            AgentProfile(
                id="agent_1",
                tenant_id="tenant_demo",
                name="客服",
                metadata_json={"owner_user_id": owner.id},
            )
        )
        db.commit()
        db.refresh(owner)
        db.expunge(owner)
    return owner, {"Authorization": f"Bearer {create_access_token(owner)}"}


def _seed_scope_history(engine, binding_id: str, scope: str) -> None:
    """bot_id scope 时期的存量:身份/会话/指针。"""
    from app.db.models import ChannelConvState

    with Session(engine) as db:
        lazy = User(
            id="lazy_scope",
            tenant_id="tenant_demo",
            username=f"wecom_{scope}_zhangsan",
            display_name="企微用户 zhangsan",
            source="wecom",
            password_hash="x",
        )
        db.add(lazy)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope=scope,
                external_user_id="zhangsan",
                staffdeck_user_id=lazy.id,
            )
        )
        db.add(
            ChatSession(
                id="s_scope",
                tenant_id="tenant_demo",
                user_id=lazy.id,
                agent_id="agent_1",
                channel="wecom",
                external_conv_id=f"wecom_{scope}_p2p_zhangsan",
                channel_binding_id=binding_id,
            )
        )
        db.add(
            ChannelConvState(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                external_conv_id=f"wecom_{scope}_p2p_zhangsan",
                current_agent_id="agent_1",
            )
        )
        db.commit()


def test_credentials_corp_id_fill_migrates_identity_sessions_and_pointer(monkeypatch) -> None:
    import app.api.channels as channels_api
    from app.db.models import ChannelConvState

    engine = _test_engine()
    owner, headers = _seed_owner(engine)
    _ = owner
    binding_id = _seed_wecom_binding(engine, bot_id="aib_bot1")
    _seed_scope_history(engine, binding_id, "aib_bot1")
    client = _make_api_client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)

    response = client.post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={
            "tenant_id": "tenant_demo",
            "bot_id": "aib_bot1",
            "secret": "bot_secret",
            "corp_id": "corpA",
        },
        headers=headers,
    )
    assert response.status_code == 200

    with Session(engine) as db:
        identity = db.exec(select(ChannelIdentity)).one()
        # 身份连续:scope 更新为 corpA,仍指向同一 User
        assert identity.external_account_scope == "corpA"
        assert identity.staffdeck_user_id == "lazy_scope"
        assert db.get(ChatSession, "s_scope").external_conv_id == "wecom_corpA_p2p_zhangsan"
        state = db.exec(select(ChannelConvState)).one()
        assert state.external_conv_id == "wecom_corpA_p2p_zhangsan"

    # 新消息按新 scope 解析到同一 User,正常路由
    binding = _load_binding(engine, binding_id)
    assert process_inbound(binding, _wecom_inbound("m_after", "你好"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].user_id == "lazy_scope"


def test_scope_migration_ignores_unreferenced_identities() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine, corp_id="corpA", bot_id="aib_bot1")
    with Session(engine) as db:
        db.add(User(id="l1", tenant_id="tenant_demo", username="wecom_aib_bot1_zhangsan", source="wecom", password_hash="x"))
        db.add(User(id="l2", tenant_id="tenant_demo", username="wecom_aib_bot1_lisi", source="wecom", password_hash="x"))
        # l1 被该 binding 的会话引用;l2 的旧 scope 身份无任何该 binding 会话引用
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="aib_bot1",
                external_user_id="zhangsan",
                staffdeck_user_id="l1",
            )
        )
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="aib_bot1",
                external_user_id="lisi",
                staffdeck_user_id="l2",
            )
        )
        binding = db.get(ChannelBinding, binding_id)
        config = dict(binding.config_json or {})
        config["corp_id"] = "corpA"
        binding.config_json = config
        db.add(binding)
        db.add(
            ChatSession(
                id="s_ref",
                tenant_id="tenant_demo",
                user_id="l1",
                agent_id="agent_1",
                channel="wecom",
                external_conv_id="wecom_aib_bot1_p2p_zhangsan",
                channel_binding_id=binding_id,
            )
        )
        db.add(
            ChatSession(
                id="s_unref",
                tenant_id="tenant_demo",
                user_id="l2",
                agent_id="agent_1",
                channel="wecom",
                external_conv_id="wecom_aib_bot1_p2p_lisi",
                channel_binding_id="chan_other",
            )
        )
        db.commit()

        from app.channels.service_identity import migrate_scope_for_binding

        stats = migrate_scope_for_binding(db, binding, "aib_bot1", "corpA")
        db.commit()

        assert stats["identities"] == 1
        assert stats["sessions"] == 1
        identities = {
            row.external_user_id: row.external_account_scope
            for row in db.exec(select(ChannelIdentity)).all()
        }
        # 被当前 binding 会话引用的身份迁移;未被引用的旧 scope 行不动
        assert identities == {"zhangsan": "corpA", "lisi": "aib_bot1"}
        assert db.get(ChatSession, "s_ref").external_conv_id == "wecom_corpA_p2p_zhangsan"
        assert db.get(ChatSession, "s_unref").external_conv_id == "wecom_aib_bot1_p2p_lisi"


def test_scope_migration_roundtrip_and_noop(monkeypatch) -> None:
    import app.api.channels as channels_api
    from app.channels import service_identity as identity_module

    engine = _test_engine()
    owner, headers = _seed_owner(engine)
    _ = owner
    binding_id = _seed_wecom_binding(engine, bot_id="aib_bot1")
    _seed_scope_history(engine, binding_id, "aib_bot1")
    client = _make_api_client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)

    url = f"/api/enterprise/channels/{binding_id}/wecom/credentials"
    # ① 补填 corp_id:迁移到 corpA
    client.post(url, json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "s1", "corp_id": "corpA"}, headers=headers)
    with Session(engine) as db:
        assert db.exec(select(ChannelIdentity)).one().external_account_scope == "corpA"
        assert db.get(ChatSession, "s_scope").external_conv_id == "wecom_corpA_p2p_zhangsan"

    # ② 显式空串去掉 corp_id:跨企业变更,400 拦截且数据零变化
    cleared = client.post(url, json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "s2", "corp_id": ""}, headers=headers)
    assert cleared.status_code == 400
    assert "删除后重新创建绑定" in cleared.json()["detail"]
    with Session(engine) as db:
        assert db.exec(select(ChannelIdentity)).one().external_account_scope == "corpA"
        assert db.get(ChatSession, "s_scope").external_conv_id == "wecom_corpA_p2p_zhangsan"

    # ②b corpA→corpB:同样 400 拦截且零变化
    changed = client.post(url, json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "s2", "corp_id": "corpB"}, headers=headers)
    assert changed.status_code == 400
    with Session(engine) as db:
        assert db.exec(select(ChannelIdentity)).one().external_account_scope == "corpA"

    # ③ 再保存一次(scope 无变化):不触发迁移
    calls: list = []
    original = identity_module.migrate_scope_for_binding
    monkeypatch.setattr(
        channels_api,
        "migrate_scope_for_binding",
        lambda db, binding, old, new: calls.append((old, new)) or original(db, binding, old, new),
    )
    client.post(url, json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "s3", "corp_id": "corpA"}, headers=headers)
    assert calls == []
    with Session(engine) as db:
        assert len(db.exec(select(ChannelIdentity)).all()) == 1
        assert db.get(ChatSession, "s_scope").external_conv_id == "wecom_corpA_p2p_zhangsan"


def test_migrate_scope_noop_when_unchanged() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine, bot_id="aib_bot1")
    with Session(engine) as db:
        from app.channels.service_identity import migrate_scope_for_binding

        binding = db.get(ChannelBinding, binding_id)
        stats = migrate_scope_for_binding(db, binding, "aib_bot1", "aib_bot1")
        assert stats == {"identities": 0, "identities_conflicted": 0, "sessions": 0, "conv_states": 0}


# ---------- 同企业多 Bot:会话与出站按 binding 隔离 ----------


def test_same_corp_two_bots_get_isolated_sessions_and_outbound() -> None:
    engine = _test_engine()
    binding_a = _seed_wecom_binding(engine, corp_id="corpX", bot_id="bot_a")
    # uq(agent_id, channel) 全局唯一:同 tenant 两绑定用不同默认员工
    binding_b = _seed_wecom_binding(engine, corp_id="corpX", bot_id="bot_b", agent_id="agent_2")

    binding = _load_binding(engine, binding_a)
    assert process_inbound(binding, _wecom_inbound("m_a", "你好 A"), db_engine=engine) is True
    binding = _load_binding(engine, binding_b)
    assert process_inbound(binding, _wecom_inbound("m_b", "你好 B"), db_engine=engine) is True

    session_a = RecordingAgentLoop.calls[0].session_id
    session_b = RecordingAgentLoop.calls[1].session_id
    assert session_a != session_b
    with Session(engine) as db:
        assert db.get(ChatSession, session_a).channel_binding_id == binding_a
        assert db.get(ChatSession, session_b).channel_binding_id == binding_b

        # 出站各自按会话直挂 binding 投递(second_incoming_binding=bot_b → outbound=bot_b)
        from app.channels.service_outbox import stage_channel_delivery
        from app.db.models import ChannelDelivery, Message

        for session_id, message_id in ((session_a, "msg_out_a"), (session_b, "msg_out_b")):
            chat_session = db.get(ChatSession, session_id)
            message = Message(
                id=message_id,
                tenant_id="tenant_demo",
                session_id=session_id,
                role="assistant",
                content="回复",
            )
            db.add(message)
            stage_channel_delivery(db, chat_session, message)
        db.commit()
        deliveries = {
            row.message_id: row.binding_id for row in db.exec(select(ChannelDelivery)).all()
        }
        assert deliveries == {"msg_out_a": binding_a, "msg_out_b": binding_b}


# ---------- scope 迁移限定当前 binding + 冲突合并一致 ----------


def test_scope_migration_narrowed_to_current_binding() -> None:
    engine = _test_engine()
    binding_a = _seed_wecom_binding(engine, bot_id="bot_a")
    binding_b = _seed_wecom_binding(engine, bot_id="bot_b", agent_id="agent_2")
    with Session(engine) as db:
        for lazy_id, conv_suffix, bid, agent, scope in (
            ("lazy_a", "zhangsan", binding_a, "agent_1", "bot_a"),
            ("lazy_b", "lisi", binding_b, "agent_2", "bot_b"),
        ):
            db.add(
                User(
                    id=lazy_id,
                    tenant_id="tenant_demo",
                    username=channel_username("tenant_demo", "wecom", conv_suffix, scope),
                    source="wecom",
                    password_hash="x",
                )
            )
            db.add(
                ChannelIdentity(
                    tenant_id="tenant_demo",
                    channel="wecom",
                    external_account_scope=scope,
                    external_user_id=conv_suffix,
                    staffdeck_user_id=lazy_id,
                )
            )
            db.add(
                ChatSession(
                    id=f"s_{conv_suffix}",
                    tenant_id="tenant_demo",
                    user_id=lazy_id,
                    agent_id=agent,
                    channel="wecom",
                    external_conv_id=f"wecom_{scope}_p2p_{conv_suffix}",
                    channel_binding_id=bid,
                )
            )
        from app.db.models import ChannelConvState

        db.add(
            ChannelConvState(
                tenant_id="tenant_demo",
                binding_id=binding_a,
                external_conv_id="wecom_bot_a_p2p_zhangsan",
                current_agent_id="agent_1",
            )
        )
        db.add(
            ChannelConvState(
                tenant_id="tenant_demo",
                binding_id=binding_b,
                external_conv_id="wecom_bot_b_p2p_lisi",
                current_agent_id="agent_2",
            )
        )
        db.commit()

        from app.channels.service_identity import migrate_scope_for_binding

        # A 首次补填企业信息(bot_a → corpY):只有 A 的身份/会话/指针迁移
        binding_a_row = db.get(ChannelBinding, binding_a)
        config = dict(binding_a_row.config_json or {})
        config["corp_id"] = "corpY"
        binding_a_row.config_json = config
        db.add(binding_a_row)
        db.commit()
        stats = migrate_scope_for_binding(db, db.get(ChannelBinding, binding_a), "bot_a", "corpY")
        db.commit()
        assert stats["identities"] == 1
        assert stats["sessions"] == 1
        assert stats["conv_states"] == 1

        identity_a = db.exec(
            select(ChannelIdentity).where(ChannelIdentity.external_user_id == "zhangsan")
        ).one()
        assert identity_a.external_account_scope == "corpY"
        assert db.get(ChatSession, "s_zhangsan").external_conv_id == "wecom_corpY_p2p_zhangsan"
        state_a = db.exec(
            select(ChannelConvState).where(ChannelConvState.binding_id == binding_a)
        ).one()
        assert state_a.external_conv_id == "wecom_corpY_p2p_zhangsan"

        # B 的身份/会话/指针零影响
        identity_b = db.exec(
            select(ChannelIdentity).where(ChannelIdentity.external_user_id == "lisi")
        ).one()
        assert identity_b.external_account_scope == "bot_b"
        assert db.get(ChatSession, "s_lisi").external_conv_id == "wecom_bot_b_p2p_lisi"
        state_b = db.exec(
            select(ChannelConvState).where(ChannelConvState.binding_id == binding_b)
        ).one()
        assert state_b.external_conv_id == "wecom_bot_b_p2p_lisi"


def test_scope_migration_conflict_merges_consistently() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine, bot_id="aib_bot1")
    with Session(engine) as db:
        db.add(User(id="lazy_old", tenant_id="tenant_demo", username=channel_username("tenant_demo", "wecom", "zhangsan", "aib_bot1"), source="wecom", password_hash="x"))
        db.add(User(id="user_web", tenant_id="tenant_demo", username="webadmin", password_hash="x"))
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="aib_bot1",
                external_user_id="zhangsan",
                staffdeck_user_id="lazy_old",
            )
        )
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="corpY",
                external_user_id="zhangsan",
                staffdeck_user_id="user_web",
            )
        )
        db.add(
            ChatSession(
                id="s_conflict",
                tenant_id="tenant_demo",
                user_id="lazy_old",
                agent_id="agent_1",
                channel="wecom",
                external_conv_id="wecom_aib_bot1_p2p_zhangsan",
                channel_binding_id=binding_id,
            )
        )
        db.add(
            MemoryRecord(
                id="mem_conflict",
                tenant_id="tenant_demo",
                user_id="lazy_old",
                username=channel_username("tenant_demo", "wecom", "zhangsan", "aib_bot1"),
                session_id="s_conflict",
                content="偏好",
            )
        )
        db.commit()

        from app.channels.service_identity import migrate_scope_for_binding

        # 首次补填企业信息(aib_bot1 → corpY):冲突合并
        binding = db.get(ChannelBinding, binding_id)
        config = dict(binding.config_json or {})
        config["corp_id"] = "corpY"
        binding.config_json = config
        db.add(binding)
        db.commit()
        stats = migrate_scope_for_binding(db, db.get(ChannelBinding, binding_id), "aib_bot1", "corpY")
        db.commit()
        assert stats["identities_conflicted"] == 1

        # 合并后身份与会话一致:同一 User、同一 conv 前缀
        rows = db.exec(select(ChannelIdentity)).all()
        assert len(rows) == 1
        assert rows[0].external_account_scope == "corpY"
        assert rows[0].staffdeck_user_id == "user_web"
        session = db.get(ChatSession, "s_conflict")
        assert session.user_id == "user_web"
        assert session.external_conv_id == "wecom_corpY_p2p_zhangsan"
        memory = db.get(MemoryRecord, "mem_conflict")
        assert memory.user_id == "user_web"
        assert memory.username == "webadmin"


# ---------- corp_id 可读取 + 不传不清空 ----------


def test_corp_id_read_and_reconfig_keeps_unless_explicit(monkeypatch) -> None:
    import app.api.channels as channels_api

    engine = _test_engine()
    owner, headers = _seed_owner(engine)
    binding_id = _seed_wecom_binding(engine, bot_id="aib_bot1")
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.status = "pending"
        binding.credentials_enc = None
        db.add(binding)
        db.commit()
    client = _make_api_client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)

    url = f"/api/enterprise/channels/{binding_id}/wecom/credentials"
    client.post(
        url,
        json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "s1", "corp_id": "corpA"},
        headers=headers,
    )
    listed = client.get(
        "/api/enterprise/channels?tenant_id=tenant_demo&agent_id=agent_1", headers=headers
    )
    assert listed.json()[0]["corp_id"] == "corpA"
    assert "s1" not in listed.text

    # 不传 corp_id 字段重新配置:不被静默清空
    client.post(
        url,
        json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "s2"},
        headers=headers,
    )
    with Session(engine) as db:
        assert db.get(ChannelBinding, binding_id).config_json["corp_id"] == "corpA"

    # 显式传空串想清除:属于跨企业变更,400 拦截,corpA 保留(与本轮禁止迁移语义一致)
    cleared = client.post(
        url,
        json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "s3", "corp_id": ""},
        headers=headers,
    )
    assert cleared.status_code == 400
    with Session(engine) as db:
        assert db.get(ChannelBinding, binding_id).config_json["corp_id"] == "corpA"


# ---------- 同 Agent 多 binding 迁移(去 uq 重建) ----------


def test_channel_bindings_multi_migration_removes_uq(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "multi.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa_text(
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
                    updated_at DATETIME,
                    CONSTRAINT uq_channel_binding_agent_channel UNIQUE (agent_id, channel)
                )
                """
            )
        )
        conn.execute(
            sa_text(
                "INSERT INTO channel_bindings (id, tenant_id, agent_id, channel, status) "
                "VALUES ('chan_1', 'tenant_demo', 'agent_1', 'wecom', 'active')"
            )
        )
    monkeypatch.setattr(database, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_sqlite_skill_schema()
    with engine.begin() as conn:
        # 重建后同 (agent_id, channel) 可重复
        conn.execute(
            sa_text(
                "INSERT INTO channel_bindings (id, tenant_id, agent_id, channel, status) "
                "VALUES ('chan_2', 'tenant_demo', 'agent_1', 'wecom', 'pending')"
            )
        )
        count = conn.execute(sa_text("SELECT COUNT(*) FROM channel_bindings")).scalar_one()
        assert count == 2

    # 幂等重跑不炸
    database._migrate_sqlite_skill_schema()
    with engine.begin() as conn:
        assert conn.execute(sa_text("SELECT COUNT(*) FROM channel_bindings")).scalar_one() == 2
        applied = conn.execute(
            sa_text("SELECT id FROM app_data_migrations WHERE id = :id"),
            {"id": database._CHANNEL_BINDINGS_MULTI_MIGRATION_ID},
        ).first()
        assert applied is not None
