from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.channels.service_intake as intake_module
import app.core.agent_loop as agent_loop_module
from app.api.auth import list_users
from app.api.channels import create_bind_code
from app.channels.service_identity import channel_username, resolve_or_provision_user
from app.channels.service_intake import process_inbound
from app.channels.service_routing import parse_command
from app.db.models import (
    ChannelBindCode,
    ChannelBinding,
    ChannelDelivery,
    ChannelIdentity,
    ChatSession,
    MemoryRecord,
    Tenant,
    User,
    utc_now,
)


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


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


def _group_message(event_id: str, text: str) -> dict:
    msg = _p2p_message(event_id, text)
    msg["group_id"] = "room_123456"
    msg["session_id"] = "room_123456"
    return msg


def _seed_binding(engine) -> str:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wechat",
            status="active",
            config_json={"ilink_bot_id": "bot_1@im.bot"},
        )
        db.add(binding)
        db.commit()
        return binding.id


def _load_binding(engine, binding_id: str) -> ChannelBinding:
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        db.expunge(binding)
        return binding


def _seed_web_user(engine, user_id: str = "user_web", username: str = "zhangsan") -> None:
    with Session(engine) as db:
        db.add(
            User(
                id=user_id,
                tenant_id="tenant_demo",
                username=username,
                display_name="张三",
                password_hash="x",
            )
        )
        db.commit()


def _seed_lazy_history(engine, lazy: User) -> None:
    """模拟绑定前的历史:懒建账号名下已有会话与记忆(另有一条群会话不应被动)。"""
    with Session(engine) as db:
        db.add(
            ChatSession(
                id="s_p2p",
                tenant_id="tenant_demo",
                user_id=lazy.id,
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_p2p_user_ab12cd34@im.wechat",
            )
        )
        db.add(
            ChatSession(
                id="s_group",
                tenant_id="tenant_demo",
                user_id="u_group_account",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_group_room_1",
            )
        )
        db.add(
            MemoryRecord(
                id="mem_1",
                tenant_id="tenant_demo",
                user_id=lazy.id,
                username=lazy.username,
                session_id="s_p2p",
                content="用户偏好靠窗座位",
            )
        )
        db.commit()


def _make_lazy_account(engine) -> User:
    with Session(engine) as db:
        lazy = User(
            id="user_lazy",
            tenant_id="tenant_demo",
            username=channel_username("tenant_demo", "wechat", "user_ab12cd34@im.wechat"),
            display_name="微信用户 ab12cd34",
            role="member",
            source="wechat",
            password_hash="x",
        )
        db.add(lazy)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wechat",
                external_user_id="user_ab12cd34@im.wechat",
                staffdeck_user_id=lazy.id,
                display_name=lazy.display_name,
            )
        )
        db.commit()
        db.refresh(lazy)
        db.expunge(lazy)
        return lazy


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


# ---------- 指令解析 ----------


def test_parse_bind_commands() -> None:
    cmd = parse_command("/绑定 123456")
    assert cmd.kind == "bind" and cmd.query == "123456"
    alias = parse_command("/BIND 654321")
    assert alias.kind == "bind" and alias.query == "654321"
    assert parse_command("/解绑").kind == "unbind"
    assert parse_command("/unbind").kind == "unbind"
    empty = parse_command("/绑定")
    assert empty.kind == "bind" and empty.query == ""


# ---------- source 列迁移与回填 ----------


def test_user_source_backfill_marks_wechat_accounts(monkeypatch, tmp_path) -> None:
    from sqlalchemy import text as sa_text

    from app.db import database

    db_path = tmp_path / "source.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa_text(
                """
                CREATE TABLE users (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR,
                    username VARCHAR,
                    display_name VARCHAR,
                    role VARCHAR NOT NULL DEFAULT 'member',
                    password_hash VARCHAR NOT NULL,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            sa_text(
                "INSERT INTO users (id, tenant_id, username, password_hash) VALUES "
                "('u1', 't', 'admin', 'x'), "
                "('u2', 't', 'wechat_user_ab12cd34@im.wechat', 'x'), "
                "('u3', 't', 'wechat_group_room_1', 'x')"
            )
        )

    monkeypatch.setattr(database, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_sqlite_skill_schema()
    with engine.begin() as conn:
        rows = dict(
            conn.execute(sa_text("SELECT id, source FROM users")).all()
        )
        assert rows == {"u1": "web", "u2": "wechat", "u3": "wechat"}

        # 幂等:回写后重跑不炸也不覆盖
        conn.execute(sa_text("UPDATE users SET source = 'web' WHERE id = 'u2'"))
    database._migrate_sqlite_skill_schema()
    with engine.begin() as conn:
        assert conn.execute(sa_text("SELECT source FROM users WHERE id = 'u2'")).scalar_one() == "web"
        applied = conn.execute(
            sa_text("SELECT id FROM app_data_migrations WHERE id = :id"),
            {"id": database._USER_SOURCE_BACKFILL_MIGRATION_ID},
        ).first()
        assert applied is not None


def test_list_users_hides_channel_accounts_by_default() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        admin = User(id="admin_user", tenant_id="tenant_demo", username="admin", role="admin", password_hash="x")
        db.add(admin)
        db.add(User(id="u_web", tenant_id="tenant_demo", username="zhangsan", password_hash="x"))
        db.add(
            User(
                id="u_lazy",
                tenant_id="tenant_demo",
                username=channel_username("tenant_demo", "wechat", "user_ab12cd34@im.wechat"),
                source="wechat",
                password_hash="x",
            )
        )
        db.commit()

        default_rows = list_users("tenant_demo", include_channel=False, current_user=admin, db=db)
        assert {row.username for row in default_rows} == {"admin", "zhangsan"}
        assert all(row.source == "web" for row in default_rows)

        all_rows = list_users("tenant_demo", include_channel=True, current_user=admin, db=db)
        assert {row.username for row in all_rows} == {
            "admin",
            "zhangsan",
            channel_username("tenant_demo", "wechat", "user_ab12cd34@im.wechat"),
        }
        lazy_row = next(row for row in all_rows if row.id == "u_lazy")
        assert lazy_row.source == "wechat"


# ---------- bind-code 生成 ----------


def test_create_bind_code_invalidates_stale_codes() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        owner = User(id="user_web", tenant_id="tenant_demo", username="zhangsan", display_name="张三", password_hash="x")
        db.add(owner)
        db.commit()

        first = create_bind_code(tenant_id="tenant_demo", current_user=owner, db=db)
        assert first.code.isdigit() and len(first.code) == 6

        second = create_bind_code(tenant_id="tenant_demo", current_user=owner, db=db)
        assert second.code.isdigit() and len(second.code) == 6

        codes = db.exec(
            select(ChannelBindCode).where(ChannelBindCode.user_id == owner.id)
        ).all()
        assert len(codes) == 2
        stale = next(row for row in codes if row.code == first.code)
        fresh = next(row for row in codes if row.id != stale.id)
        assert stale.expires_at <= utc_now()
        assert fresh.expires_at > utc_now()


# ---------- /绑定 全流程 ----------


def _issue_code(engine, user_id: str = "user_web", code: str = "123456", *, expired: bool = False) -> None:
    with Session(engine) as db:
        db.add(
            ChannelBindCode(
                tenant_id="tenant_demo",
                user_id=user_id,
                code=code,
                expires_at=utc_now() + timedelta(minutes=-1 if expired else 10),
            )
        )
        db.commit()


def _notice_texts(engine) -> list[str]:
    with Session(engine) as db:
        rows = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "notice")
        ).all()
        return [row.text for row in rows]


def test_bind_success_migrates_history_and_marks_code_used() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_web_user(engine)
    lazy = _make_lazy_account(engine)
    _seed_lazy_history(engine, lazy)
    _issue_code(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_b1", "/绑定 123456"), db_engine=engine) is False

    notices = _notice_texts(engine)
    assert any("绑定成功" in text and "「张三」" in text for text in notices)

    with Session(engine) as db:
        identity = db.exec(select(ChannelIdentity)).one()
        assert identity.staffdeck_user_id == "user_web"
        assert db.get(ChatSession, "s_p2p").user_id == "user_web"
        memory = db.get(MemoryRecord, "mem_1")
        assert memory.user_id == "user_web"
        assert memory.username == "zhangsan"
        # 群会话属于群账号,不动
        assert db.get(ChatSession, "s_group").user_id == "u_group_account"
        record = db.exec(select(ChannelBindCode)).one()
        assert record.used_at is not None

    # 绑定后身份解析命中码主账号
    with Session(engine) as db:
        user = resolve_or_provision_user(db, "tenant_demo", "wechat", "user_ab12cd34@im.wechat", None)
        assert user.id == "user_web"

    # 绑定后的下一条消息,会话归码主账号
    binding = _load_binding(engine, binding_id)
    assert process_inbound(binding, _p2p_message("evt_b2", "你好"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].user_id == "user_web"


def test_bind_with_wrong_or_expired_code() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_web_user(engine)
    _issue_code(engine, code="123456", expired=True)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_b3", "/绑定 999999"), db_engine=engine) is False
    assert process_inbound(binding, _p2p_message("evt_b4", "/绑定 123456"), db_engine=engine) is False

    notices = _notice_texts(engine)
    assert len(notices) == 2
    assert all("无效或已过期" in text for text in notices)
    with Session(engine) as db:
        assert db.exec(select(ChannelIdentity)).all() == []


def test_bind_rejected_when_identity_bound_to_other_web_account() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_web_user(engine, user_id="user_web", username="zhangsan")
    _seed_web_user(engine, user_id="user_web2", username="lisi")
    # 该微信已绑定到李四
    with Session(engine) as db:
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wechat",
                external_user_id="user_ab12cd34@im.wechat",
                staffdeck_user_id="user_web2",
                display_name="李四",
            )
        )
        db.commit()
    _issue_code(engine, user_id="user_web", code="123456")
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_b5", "/绑定 123456"), db_engine=engine) is False

    notices = _notice_texts(engine)
    assert any("已绑定" in text and "/解绑" in text for text in notices)
    with Session(engine) as db:
        identity = db.exec(select(ChannelIdentity)).one()
        assert identity.staffdeck_user_id == "user_web2"
        record = db.exec(select(ChannelBindCode)).one()
        assert record.used_at is None


def test_bind_command_not_supported_in_group() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _group_message("evt_b6", "/绑定 123456"), db_engine=engine) is False
    notices = _notice_texts(engine)
    assert any("私聊" in text for text in notices)


# ---------- /解绑 ----------


def test_unbind_moves_history_back_to_lazy_account() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_web_user(engine)
    lazy = _make_lazy_account(engine)
    _seed_lazy_history(engine, lazy)
    _issue_code(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_u1", "/绑定 123456"), db_engine=engine) is False
    assert process_inbound(binding, _p2p_message("evt_u2", "/解绑"), db_engine=engine) is False

    notices = _notice_texts(engine)
    assert any("已解绑" in text and "「张三」" in text for text in notices)

    with Session(engine) as db:
        identity = db.exec(select(ChannelIdentity)).one()
        assert identity.staffdeck_user_id == "user_lazy"
        assert db.get(ChatSession, "s_p2p").user_id == "user_lazy"
        memory = db.get(MemoryRecord, "mem_1")
        assert memory.user_id == "user_lazy"
        assert memory.username == channel_username("tenant_demo", "wechat", "user_ab12cd34@im.wechat")

    # 解绑后身份解析回到懒建账号
    with Session(engine) as db:
        user = resolve_or_provision_user(db, "tenant_demo", "wechat", "user_ab12cd34@im.wechat", None)
        assert user.id == "user_lazy"


def test_unbind_creates_lazy_account_when_missing() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_web_user(engine)
    # 身份直接绑在 web 账号(懒建账号不存在)
    with Session(engine) as db:
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wechat",
                external_user_id="user_ab12cd34@im.wechat",
                staffdeck_user_id="user_web",
                display_name="张三",
            )
        )
        db.commit()
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_u3", "/解绑"), db_engine=engine) is False

    with Session(engine) as db:
        lazy = db.exec(
            select(User).where(
                User.username == channel_username("tenant_demo", "wechat", "user_ab12cd34@im.wechat")
            )
        ).one()
        assert lazy.source == "wechat"
        identity = db.exec(select(ChannelIdentity)).one()
        assert identity.staffdeck_user_id == lazy.id


def test_unbind_without_binding_is_noop() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    lazy = _make_lazy_account(engine)
    _ = lazy
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_u4", "/解绑"), db_engine=engine) is False
    notices = _notice_texts(engine)
    assert any("未绑定" in text for text in notices)


# ---------- 页面侧身份绑定查询与解绑 ----------


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


def _auth(user: User) -> dict[str, str]:
    from app.security.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user)}"}


def _seed_web_users(engine) -> dict[str, User]:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        web = User(id="user_web", tenant_id="tenant_demo", username="zhangsan", display_name="张三", password_hash="x")
        other = User(id="user_other", tenant_id="tenant_demo", username="lisi", display_name="李四", password_hash="x")
        db.add(web)
        db.add(other)
        db.commit()
        for user in (web, other):
            db.refresh(user)
            db.expunge(user)
        return {"web": web, "other": other}


def _seed_bound_state(engine) -> None:
    """web 账号已绑定微信身份的终态:identity 指 web,私聊会话与记忆在 web 名下。"""
    with Session(engine) as db:
        db.add(
            User(
                id="user_lazy",
                tenant_id="tenant_demo",
                username=channel_username("tenant_demo", "wechat", "user_ab12cd34@im.wechat"),
                display_name="微信用户 ab12cd34",
                source="wechat",
                password_hash="x",
            )
        )
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wechat",
                external_user_id="user_ab12cd34@im.wechat",
                staffdeck_user_id="user_web",
                display_name="张三",
            )
        )
        db.add(
            ChatSession(
                id="s_p2p",
                tenant_id="tenant_demo",
                user_id="user_web",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_p2p_user_ab12cd34@im.wechat",
            )
        )
        db.add(
            MemoryRecord(
                id="mem_1",
                tenant_id="tenant_demo",
                user_id="user_web",
                username="zhangsan",
                session_id="s_p2p",
                content="用户偏好靠窗座位",
            )
        )
        db.commit()


def test_my_identity_bindings_query() -> None:
    engine = _test_engine()
    users = _seed_web_users(engine)
    _seed_bound_state(engine)
    client = _make_api_client(engine)

    bound = client.get(
        "/api/enterprise/channels/my-identity-bindings?tenant_id=tenant_demo",
        headers=_auth(users["web"]),
    )
    assert bound.status_code == 200
    rows = bound.json()
    assert len(rows) == 1
    assert rows[0]["channel"] == "wechat"
    assert rows[0]["external_user_id"] == "user_ab12cd34@im.wechat"
    assert rows[0]["display_name"] == "张三"
    assert rows[0]["bound_at"]

    # 他人不可见
    other = client.get(
        "/api/enterprise/channels/my-identity-bindings?tenant_id=tenant_demo",
        headers=_auth(users["other"]),
    )
    assert other.status_code == 200
    assert other.json() == []


def test_my_identity_bindings_empty_when_unbound() -> None:
    engine = _test_engine()
    users = _seed_web_users(engine)
    client = _make_api_client(engine)

    response = client.get(
        "/api/enterprise/channels/my-identity-bindings?tenant_id=tenant_demo",
        headers=_auth(users["web"]),
    )
    assert response.status_code == 200
    assert response.json() == []


def test_delete_my_identity_binding_unbinds_like_command() -> None:
    engine = _test_engine()
    users = _seed_web_users(engine)
    _seed_bound_state(engine)
    client = _make_api_client(engine)

    deleted = client.delete(
        "/api/enterprise/channels/my-identity-bindings/wechat?tenant_id=tenant_demo",
        headers=_auth(users["web"]),
    )
    assert deleted.status_code == 204

    with Session(engine) as db:
        identity = db.exec(select(ChannelIdentity)).one()
        assert identity.staffdeck_user_id == "user_lazy"
        assert db.get(ChatSession, "s_p2p").user_id == "user_lazy"
        memory = db.get(MemoryRecord, "mem_1")
        assert memory.user_id == "user_lazy"
        assert memory.username == channel_username("tenant_demo", "wechat", "user_ab12cd34@im.wechat")

    # 再删一次 → 404
    again = client.delete(
        "/api/enterprise/channels/my-identity-bindings/wechat?tenant_id=tenant_demo",
        headers=_auth(users["web"]),
    )
    assert again.status_code == 404


def test_delete_my_identity_binding_404_and_tenant_mismatch() -> None:
    engine = _test_engine()
    users = _seed_web_users(engine)
    client = _make_api_client(engine)

    missing = client.delete(
        "/api/enterprise/channels/my-identity-bindings/wechat?tenant_id=tenant_demo",
        headers=_auth(users["web"]),
    )
    assert missing.status_code == 404

    _seed_bound_state(engine)
    mismatch = client.delete(
        "/api/enterprise/channels/my-identity-bindings/wechat?tenant_id=tenant_other",
        headers=_auth(users["web"]),
    )
    assert mismatch.status_code == 403


# ---------- 企微(wecom)侧 /绑定 /解绑 链路 ----------


def _wecom_p2p_message(event_id: str, text: str) -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": f"req_{event_id}"},
        "body": {
            "msgid": event_id,
            "aibotid": "aib_bot1",
            "chattype": "single",
            "from": {"userid": "zhangsan"},
            "msgtype": "text",
            "text": {"content": text},
        },
    }


def _wecom_group_message(event_id: str, text: str) -> dict:
    msg = _wecom_p2p_message(event_id, text)
    msg["body"]["chatid"] = "wr_room1"
    msg["body"]["chattype"] = "group"
    return msg


def _wecom_inbound(event_id: str, text: str, *, group: bool = False):
    from app.channels.adapters.wecom import normalize_wecom_frame

    frame = _wecom_group_message(event_id, text) if group else _wecom_p2p_message(event_id, text)
    return normalize_wecom_frame(frame)


def _seed_wecom_binding(engine) -> str:
    with Session(engine) as db:
        if not db.get(Tenant, "tenant_demo"):
            db.add(Tenant(id="tenant_demo", name="Demo"))
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wecom",
            status="active",
            config_json={"bot_id": "aib_bot1"},
        )
        db.add(binding)
        db.commit()
        return binding.id


def _make_wecom_lazy_account(engine) -> User:
    with Session(engine) as db:
        lazy = User(
            id="user_wecom_lazy",
            tenant_id="tenant_demo",
            username=channel_username("tenant_demo", "wecom", "zhangsan", "aib_bot1"),
            display_name="企微用户 zhangsan",
            role="member",
            source="wecom",
            password_hash="x",
        )
        db.add(lazy)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="aib_bot1",
                external_user_id="zhangsan",
                staffdeck_user_id=lazy.id,
                display_name=lazy.display_name,
            )
        )
        db.commit()
        db.refresh(lazy)
        db.expunge(lazy)
        return lazy


def _seed_wecom_lazy_history(engine, lazy: User) -> None:
    with Session(engine) as db:
        db.add(
            ChatSession(
                id="s_wecom_p2p",
                tenant_id="tenant_demo",
                user_id=lazy.id,
                agent_id="agent_1",
                channel="wecom",
                external_conv_id="wecom_aib_bot1_p2p_zhangsan",
            )
        )
        db.add(
            ChatSession(
                id="s_wecom_group",
                tenant_id="tenant_demo",
                user_id="u_wecom_group_account",
                agent_id="agent_1",
                channel="wecom",
                external_conv_id="wecom_group_wr_room1",
            )
        )
        db.add(
            MemoryRecord(
                id="mem_wecom_1",
                tenant_id="tenant_demo",
                user_id=lazy.id,
                username=lazy.username,
                session_id="s_wecom_p2p",
                content="用户偏好靠窗座位",
            )
        )
        db.commit()


def test_wecom_bind_success_full_chain() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    _seed_web_user(engine)
    lazy = _make_wecom_lazy_account(engine)
    _seed_wecom_lazy_history(engine, lazy)
    _issue_code(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _wecom_inbound("evt_wb1", "/绑定 123456"), db_engine=engine) is False

    notices = _notice_texts(engine)
    assert any("绑定成功" in text and "「张三」" in text for text in notices)

    with Session(engine) as db:
        identity = db.exec(select(ChannelIdentity)).one()
        assert identity.staffdeck_user_id == "user_web"
        assert db.get(ChatSession, "s_wecom_p2p").user_id == "user_web"
        memory = db.get(MemoryRecord, "mem_wecom_1")
        assert memory.user_id == "user_web"
        assert memory.username == "zhangsan"
        # 群会话属于群账号,不动
        assert db.get(ChatSession, "s_wecom_group").user_id == "u_wecom_group_account"
        record = db.exec(select(ChannelBindCode)).one()
        assert record.used_at is not None

    # 绑定后身份解析命中码主账号,下一条消息归属码主
    with Session(engine) as db:
        user = resolve_or_provision_user(db, "tenant_demo", "wecom", "zhangsan", None, "aib_bot1")
        assert user.id == "user_web"
    binding = _load_binding(engine, binding_id)
    assert process_inbound(binding, _wecom_inbound("evt_wb2", "你好"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].user_id == "user_web"


def test_wecom_unbind_moves_history_back() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    _seed_web_user(engine)
    lazy = _make_wecom_lazy_account(engine)
    _seed_wecom_lazy_history(engine, lazy)
    _issue_code(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _wecom_inbound("evt_wb3", "/绑定 123456"), db_engine=engine) is False
    assert process_inbound(binding, _wecom_inbound("evt_wb4", "/解绑"), db_engine=engine) is False

    notices = _notice_texts(engine)
    assert any("已解绑" in text and "「张三」" in text for text in notices)

    with Session(engine) as db:
        identity = db.exec(select(ChannelIdentity)).one()
        assert identity.staffdeck_user_id == "user_wecom_lazy"
        assert db.get(ChatSession, "s_wecom_p2p").user_id == "user_wecom_lazy"
        memory = db.get(MemoryRecord, "mem_wecom_1")
        assert memory.user_id == "user_wecom_lazy"
        assert memory.username == channel_username("tenant_demo", "wecom", "zhangsan", "aib_bot1")

    with Session(engine) as db:
        user = resolve_or_provision_user(db, "tenant_demo", "wecom", "zhangsan", None, "aib_bot1")
        assert user.id == "user_wecom_lazy"


def test_wecom_bind_command_not_supported_in_group() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _wecom_inbound("evt_wb5", "/绑定 123456", group=True), db_engine=engine) is False
    notices = _notice_texts(engine)
    assert any("私聊" in text for text in notices)


# ---------- 企微页面侧解绑携带 scope(假 204 修复) ----------


def test_wecom_delete_my_identity_binding_moves_data_back() -> None:
    engine = _test_engine()
    users = _seed_web_users(engine)
    # 企微身份(scope=corpA)已绑定到 web 账号,会话与记忆在 web 名下
    with Session(engine) as db:
        lazy = User(
            id="user_wecom_lazy",
            tenant_id="tenant_demo",
            username=channel_username("tenant_demo", "wecom", "zhangsan", "corpA"),
            display_name="企微用户 zhangsan",
            source="wecom",
            password_hash="x",
        )
        db.add(lazy)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="corpA",
                external_user_id="zhangsan",
                staffdeck_user_id=users["web"].id,
                display_name="张三",
            )
        )
        db.add(
            ChatSession(
                id="s_wecom_bound",
                tenant_id="tenant_demo",
                user_id=users["web"].id,
                agent_id="agent_1",
                channel="wecom",
                external_conv_id="wecom_corpA_p2p_zhangsan",
            )
        )
        db.add(
            MemoryRecord(
                id="mem_wecom_bound",
                tenant_id="tenant_demo",
                user_id=users["web"].id,
                username=users["web"].username,
                session_id="s_wecom_bound",
                content="偏好",
            )
        )
        db.commit()

    client = _make_api_client(engine)
    deleted = client.delete(
        "/api/enterprise/channels/my-identity-bindings/wecom?tenant_id=tenant_demo",
        headers=_auth(users["web"]),
    )
    assert deleted.status_code == 204

    with Session(engine) as db:
        identity = db.exec(select(ChannelIdentity)).one()
        assert identity.staffdeck_user_id == "user_wecom_lazy"
        assert db.get(ChatSession, "s_wecom_bound").user_id == "user_wecom_lazy"
        memory = db.get(MemoryRecord, "mem_wecom_bound")
        assert memory.user_id == "user_wecom_lazy"
        assert memory.username == channel_username("tenant_demo", "wecom", "zhangsan", "corpA")
