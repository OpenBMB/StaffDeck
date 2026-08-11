from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.api.channels as channels_api
from app.channels.crypto import decrypt_channel_secret, encrypt_channel_secret
from app.channels.schema import channel_binding_read
from app.db import get_session
from app.db.models import (
    AgentProfile,
    ChannelBinding,
    ChannelDelivery,
    ChannelInboundEvent,
    Message,
    Tenant,
    User,
    utc_now,
)
from app.security.auth import create_access_token


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _make_client(engine):
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
        other = User(id="user_other", tenant_id="tenant_demo", username="other", password_hash="x")
        admin = User(id="user_admin", tenant_id="tenant_demo", username="admin", role="admin", password_hash="x")
        agent = AgentProfile(
            id="agent_1",
            tenant_id="tenant_demo",
            name="客服员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        db.add_all([owner, other, admin, agent])
        db.commit()
        for user in (owner, other, admin):
            db.refresh(user)
            db.expunge(user)
        return {"owner": owner, "other": other, "admin": admin}


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user)}"}


def _seed_binding(engine, *, agent_id: str = "agent_1", status: str = "pending") -> str:
    with Session(engine) as db:
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id=agent_id,
            channel="wechat",
            status=status,
            created_by_user_id="user_owner",
        )
        db.add(binding)
        db.commit()
        return binding.id


def test_endpoints_require_authentication() -> None:
    engine = _test_engine()
    client = _make_client(engine)
    response = client.get("/api/enterprise/channels?tenant_id=tenant_demo")
    assert response.status_code == 401


def test_non_creator_cannot_create_binding() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    client = _make_client(engine)

    response = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "agent_id": "agent_1", "channel": "wechat"},
        headers=_auth(users["other"]),
    )
    assert response.status_code == 403

    created = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "agent_id": "agent_1", "channel": "wechat"},
        headers=_auth(users["owner"]),
    )
    assert created.status_code == 200
    assert created.json()["status"] == "pending"
    assert created.json()["channel"] == "wechat"

    by_admin = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "agent_id": "agent_1", "channel": "wechat"},
        headers=_auth(users["admin"]),
    )
    assert by_admin.status_code == 200


def test_unsupported_channel_rejected() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    client = _make_client(engine)

    response = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "agent_id": "agent_1", "channel": "slack"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 400


def test_tenant_mismatch_forbidden() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    client = _make_client(engine)

    response = client.get(
        "/api/enterprise/channels?tenant_id=tenant_other",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 403


def test_binding_read_never_leaks_credentials() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine, status="active")
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.credentials_enc = "enc:super_secret_bot_token"
        binding.config_json = {"ilink_bot_id": "bot@im.bot", "baseurl": "https://ilinkai.weixin.qq.com"}
        db.add(binding)
        db.commit()

    client = _make_client(engine)
    response = client.get(
        "/api/enterprise/channels?tenant_id=tenant_demo&agent_id=agent_1",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert "super_secret_bot_token" not in response.text
    assert "credentials" not in payload[0]
    assert payload[0]["ilink_bot_id"] == "bot@im.bot"


class _FakeWeChatClient:
    _DEFAULT_STATUS_RESPONSE: dict = {
        "status": "confirmed",
        "bot_token": "ilinkbot_secret_token",
        "ilink_bot_id": "bot@im.bot",
        "ilink_user_id": "user@im.wechat",
        "baseurl": "https://ilinkai.weixin.qq.com",
    }
    instances: list["_FakeWeChatClient"] = []
    qrcode_status_response: dict = dict(_DEFAULT_STATUS_RESPONSE)

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.qrcode_calls: list[dict] = []
        self.status_calls: list[dict] = []
        type(self).instances.append(self)

    @classmethod
    def reset(cls, status_response: dict | None = None) -> None:
        cls.instances = []
        cls.qrcode_status_response = (
            status_response if status_response is not None else dict(cls._DEFAULT_STATUS_RESPONSE)
        )

    def get_bot_qrcode(self, local_token_list: list[str] | None = None) -> dict:
        self.qrcode_calls.append({"local_token_list": local_token_list})
        return {"qrcode": "qrc_1", "qrcode_img_content": "https://weixin.qq.com/x/abc"}

    def get_qrcode_status(self, qrcode: str, verify_code: str | None = None, **kwargs) -> dict:
        self.status_calls.append({"qrcode": qrcode, "verify_code": verify_code})
        return dict(type(self).qrcode_status_response)


def test_qrcode_confirm_activates_binding(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)
    _FakeWeChatClient.reset()
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)
    # 测试中不启动真实长轮询线程
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)

    qrcode = client.post(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert qrcode.status_code == 200
    assert qrcode.json()["qrcode"] == "qrc_1"

    status = client.get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
        headers=_auth(users["owner"]),
    )
    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == "confirmed"
    assert payload["binding"]["status"] == "active"
    assert payload["binding"]["ilink_bot_id"] == "bot@im.bot"
    # 任何响应都不得携带 bot_token 明文
    assert "ilinkbot_secret_token" not in status.text

    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.status == "active"
        assert binding.credentials_enc
        assert "ilinkbot_secret_token" not in binding.credentials_enc
        read = channel_binding_read(db, binding)
        assert "ilinkbot_secret_token" not in read.model_dump_json()


def test_qrcode_confirm_timeout_keeps_pending_config(monkeypatch) -> None:
    import app.channels

    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    calls: list[str] = []

    class SpyManager:
        def pause_binding(self, bid):
            calls.append(f"pause:{bid}")

        def wait_binding_stopped(self, bid, timeout_seconds=5.0):
            calls.append(f"wait:{bid}")
            return False

        def resume_binding(self, bid, *, start=True):
            calls.append(f"resume:{bid}:{start}")

    _FakeWeChatClient.reset()
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: True)
    monkeypatch.setattr(app.channels, "get_wechat_poll_manager", lambda: SpyManager())

    response = _make_client(engine).get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
        headers=_auth(users["owner"]),
    )

    assert response.status_code == 409
    assert calls == [
        f"pause:{binding_id}",
        f"wait:{binding_id}",
        f"resume:{binding_id}:False",
    ]
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.status == "pending"
        assert binding.credentials_enc is None
        assert binding.config_revision == 0


def test_binded_redirect_commit_failure_restores_old_ingress(monkeypatch) -> None:
    import app.channels

    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine, status="active")
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.credentials_enc = encrypt_channel_secret("old_token")
        binding.config_json = {"ilink_bot_id": "bot@im.bot"}
        binding.external_account_key = "wechat:ilink_bot:bot@im.bot"
        db.add(binding)
        db.commit()
    calls: list[str] = []

    class SpyManager:
        def pause_binding(self, bid):
            calls.append(f"pause:{bid}")

        def wait_binding_stopped(self, bid, timeout_seconds=5.0):
            calls.append(f"wait:{bid}")
            return True

        def resume_binding(self, bid, *, start=True):
            calls.append(f"resume:{bid}:{start}")

    original_commit = Session.commit
    failed = False

    def fail_first_commit(session):
        nonlocal failed
        if not failed:
            failed = True
            raise IntegrityError("forced", {}, RuntimeError("forced"))
        return original_commit(session)

    _FakeWeChatClient.reset({"status": "binded_redirect"})
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: True)
    monkeypatch.setattr(app.channels, "get_wechat_poll_manager", lambda: SpyManager())
    monkeypatch.setattr(Session, "commit", fail_first_commit)

    response = _make_client(engine).get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
        headers=_auth(users["owner"]),
    )

    assert response.status_code == 409
    assert calls == [
        f"pause:{binding_id}",
        f"wait:{binding_id}",
        f"resume:{binding_id}:True",
    ]
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert decrypt_channel_secret(binding.credentials_enc) == "old_token"
        assert binding.config_revision == 0


def test_qrcode_endpoints_reject_non_creator(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)

    response = client.post(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode?tenant_id=tenant_demo",
        headers=_auth(users["other"]),
    )
    assert response.status_code == 403


def test_delete_binding(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine, status="active")
    client = _make_client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)
    with Session(engine) as db:
        db.add(
            ChannelDelivery(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                session_id="deleted_session",
                message_id="deleted_message",
                target_json={},
                text="pending reply",
                status="pending",
                next_attempt_at=utc_now(),
                idempotency_key="deleted_message",
            )
        )
        for status in ("received", "processing"):
            db.add(
                ChannelInboundEvent(
                    tenant_id="tenant_demo",
                    binding_id=binding_id,
                    channel="wechat",
                    event_id=f"event_{status}",
                    status=status,
                    processor_run_id="old-process" if status == "processing" else None,
                )
            )
        db.add(
            ChannelDelivery(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                session_id="deleted_session",
                message_id="sending_message",
                target_json={},
                text="in-flight reply",
                status="sending",
                attempts=1,
                first_attempt_at=utc_now(),
                idempotency_key="sending_message",
            )
        )
        db.commit()

    forbidden = client.delete(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        headers=_auth(users["other"]),
    )
    assert forbidden.status_code == 403

    deleted = client.delete(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert deleted.status_code == 204
    with Session(engine) as db:
        assert db.get(ChannelBinding, binding_id) is None
        deliveries = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.binding_id == binding_id)
        ).all()
        assert len(deliveries) == 2
        assert all(row.status == "failed" for row in deliveries)
        errors = {row.message_id: row.last_error for row in deliveries}
        assert errors == {
            "deleted_message": "binding_deleted",
            "sending_message": "binding_deleted_remote_unknown",
        }
        assert all(row.next_attempt_at is None for row in deliveries)
        events = db.exec(
            select(ChannelInboundEvent).where(ChannelInboundEvent.binding_id == binding_id)
        ).all()
        assert {row.event_id: row.error for row in events} == {
            "event_received": "binding_deleted",
            "event_processing": "binding_deleted_incomplete_turn",
        }
        assert all(row.status == "failed" for row in events)

    audit = client.get(
        "/api/enterprise/channels/delivery-audit",
        params={"tenant_id": "tenant_demo", "binding_id": binding_id},
        headers=_auth(users["admin"]),
    )
    assert audit.status_code == 200
    assert audit.json()["total"] == 2
    assert audit.json()["items"][0]["binding_id"] == binding_id

    forbidden_audit = client.get(
        "/api/enterprise/channels/delivery-audit",
        params={"tenant_id": "tenant_demo"},
        headers=_auth(users["owner"]),
    )
    assert forbidden_audit.status_code == 403


def test_deliveries_listing(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine, status="active")
    with Session(engine) as db:
        db.add(
            ChannelDelivery(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                session_id="session_1",
                message_id="msg_1",
                target_json={"to_user_id": "u1", "context_token": "ctx"},
                kind="reply",
                text="回复",
                status="delivered",
                idempotency_key="msg_1",
                next_attempt_at=utc_now(),
            )
        )
        db.commit()

    client = _make_client(engine)
    response = client.get(
        f"/api/enterprise/channels/{binding_id}/deliveries?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["offset"] == 0
    assert payload["limit"] == 50
    rows = payload["items"]
    assert len(rows) == 1
    assert rows[0]["status"] == "delivered"
    assert rows[0]["kind"] == "reply"

    forbidden = client.get(
        f"/api/enterprise/channels/{binding_id}/deliveries?tenant_id=tenant_demo",
        headers=_auth(users["other"]),
    )
    assert forbidden.status_code == 403


def test_qrcode_passes_existing_credentials_in_local_token_list(monkeypatch) -> None:
    from app.channels.crypto import encrypt_channel_secret

    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.credentials_enc = encrypt_channel_secret("old_secret_token")
        db.add(binding)
        db.commit()

    client = _make_client(engine)
    _FakeWeChatClient.reset()
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)

    response = client.post(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    assert _FakeWeChatClient.instances[0].qrcode_calls == [
        {"local_token_list": ["old_secret_token"]}
    ]


def test_qrcode_without_credentials_sends_empty_token_list(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)
    _FakeWeChatClient.reset()
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)

    response = client.post(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    assert _FakeWeChatClient.instances[0].qrcode_calls == [{"local_token_list": []}]


def test_qrcode_status_passthrough_and_verify_code(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)
    _FakeWeChatClient.reset({"status": "need_verifycode"})
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)

    response = client.get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1", "verify_code": "8823"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "need_verifycode"
    assert response.json()["binding"] is None
    assert _FakeWeChatClient.instances[0].status_calls == [
        {"qrcode": "qrc_1", "verify_code": "8823"}
    ]


def test_scaned_but_redirect_stores_and_reuses_redirect_baseurl(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)
    _FakeWeChatClient.reset({"status": "scaned_but_redirect", "redirect_host": "szilinkai.weixin.qq.com"})
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)

    response = client.get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "scaned_but_redirect"
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.config_json["qrcode_redirect_baseurl"] == "https://szilinkai.weixin.qq.com"

    # 后续轮询应使用 redirect 后的域名
    _FakeWeChatClient.reset({"status": "wait"})
    client.get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
        headers=_auth(users["owner"]),
    )
    assert _FakeWeChatClient.instances[0].base_url == "https://szilinkai.weixin.qq.com"


def test_confirmed_clears_redirect_baseurl(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.config_json = {"qrcode_redirect_baseurl": "https://szilinkai.weixin.qq.com"}
        db.add(binding)
        db.commit()

    client = _make_client(engine)
    _FakeWeChatClient.reset()  # 默认 confirmed 响应
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)

    response = client.get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.status == "active"
        assert "qrcode_redirect_baseurl" not in (binding.config_json or {})


def test_binded_redirect_activates_with_existing_credentials(monkeypatch) -> None:
    from app.channels.crypto import encrypt_channel_secret

    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine, status="expired")
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.credentials_enc = encrypt_channel_secret("still_valid_token")
        binding.config_json = {"session_expired": True, "ilink_bot_id": "bot@im.bot"}
        db.add(binding)
        db.commit()

    client = _make_client(engine)
    _FakeWeChatClient.reset({"status": "binded_redirect"})
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)

    response = client.get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "confirmed"
    assert payload["binding"]["status"] == "active"
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.status == "active"
        assert binding.config_json["session_expired"] is False


def test_binded_redirect_without_credentials_passthrough(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)
    _FakeWeChatClient.reset({"status": "binded_redirect"})
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)

    response = client.get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "binded_redirect"
    assert response.json()["binding"] is None
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.status == "pending"


def test_list_bindings_visibility_scoped_for_non_admin() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    with Session(engine) as db:
        db.add(
            AgentProfile(
                id="agent_2",
                tenant_id="tenant_demo",
                name="财务员工",
                metadata_json={"owner_user_id": users["owner"].id},
            )
        )
        own = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wechat",
            status="pending",
            created_by_user_id=users["owner"].id,
        )
        others = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_2",
            channel="wechat",
            status="pending",
            created_by_user_id=users["other"].id,
        )
        db.add(own)
        db.add(others)
        db.commit()
        own_id, other_id = own.id, others.id

    client = _make_client(engine)
    admin_list = client.get("/api/enterprise/channels?tenant_id=tenant_demo", headers=_auth(users["admin"]))
    assert admin_list.status_code == 200
    assert {row["id"] for row in admin_list.json()} == {own_id, other_id}

    owner_list = client.get("/api/enterprise/channels?tenant_id=tenant_demo", headers=_auth(users["owner"]))
    assert owner_list.status_code == 200
    assert {row["id"] for row in owner_list.json()} == {own_id}

    other_list = client.get("/api/enterprise/channels?tenant_id=tenant_demo", headers=_auth(users["other"]))
    assert other_list.status_code == 200
    assert {row["id"] for row in other_list.json()} == {other_id}


def test_list_bindings_exposes_creator_name() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    with Session(engine) as db:
        owner = db.get(User, users["owner"].id)
        assert owner is not None
        owner.display_name = "张三"
        db.add(owner)
        # 创建者已删除的存量绑定:创建人展示应回退为空而不是报错
        orphan = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wechat",
            status="pending",
            created_by_user_id="user_deleted",
        )
        db.add(orphan)
        db.commit()
        orphan_id = orphan.id

    owned_id = _seed_binding(engine)
    client = _make_client(engine)
    response = client.get("/api/enterprise/channels?tenant_id=tenant_demo", headers=_auth(users["admin"]))
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()}
    assert rows[owned_id]["created_by_name"] == "张三"
    assert rows[orphan_id]["created_by_name"] is None


def test_create_binding_returns_creator_name() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    client = _make_client(engine)
    response = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "agent_id": "agent_1", "channel": "wechat"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    assert response.json()["created_by_user_id"] == users["owner"].id
    assert response.json()["created_by_name"] == "owner"


def test_list_bindings_with_agent_id_unchanged() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    with Session(engine) as db:
        db.add(
            AgentProfile(
                id="agent_2",
                tenant_id="tenant_demo",
                name="财务员工",
                metadata_json={"owner_user_id": users["owner"].id},
            )
        )
        others = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_2",
            channel="wechat",
            status="pending",
            created_by_user_id=users["other"].id,
        )
        db.add(others)
        db.commit()
        other_id = others.id

    client = _make_client(engine)
    # 带 agent_id 时走 viewer 校验,不按创建者过滤:owner 可见他人创建的 agent_2 绑定
    owner_scoped = client.get(
        "/api/enterprise/channels?tenant_id=tenant_demo&agent_id=agent_2",
        headers=_auth(users["owner"]),
    )
    assert owner_scoped.status_code == 200
    assert {row["id"] for row in owner_scoped.json()} == {other_id}

    # 非 viewer 依旧 403
    forbidden = client.get(
        "/api/enterprise/channels?tenant_id=tenant_demo&agent_id=agent_1",
        headers=_auth(users["other"]),
    )
    assert forbidden.status_code == 403


def _seed_conversations(engine, binding_id: str) -> None:
    from datetime import datetime

    from app.db.models import ChannelIdentity, ChatSession, Message

    with Session(engine) as db:
        db.add(User(id="u_wx1", tenant_id="tenant_demo", username="wechat_u1", display_name="兜底名", password_hash="x"))
        db.add(User(id="u_grp", tenant_id="tenant_demo", username="wechat_group_room_1", password_hash="x"))
        db.add(User(id="u_legacy", tenant_id="tenant_demo", username="wechat_ulegacy", display_name="老用户", password_hash="x"))
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wechat",
                external_user_id="u1",
                staffdeck_user_id="u_wx1",
                display_name="微信用户 ab12cd34",
            )
        )
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wechat",
                external_user_id="group_room_1",
                staffdeck_user_id="u_grp",
                display_name="微信群聊 room_1",
            )
        )
        # 直挂会话:私聊 + 群聊
        db.add(
            ChatSession(
                id="s_p2p",
                tenant_id="tenant_demo",
                user_id="u_wx1",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_p2p_u1",
                channel_binding_id=binding_id,
                updated_at=datetime(2026, 7, 18, 10, 0, 0),
            )
        )
        db.add(
            ChatSession(
                id="s_group",
                tenant_id="tenant_demo",
                user_id="u_grp",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_group_room_1",
                channel_binding_id=binding_id,
                updated_at=datetime(2026, 7, 18, 12, 0, 0),
            )
        )
        # legacy 会话:无 binding_id,但 channel/external_conv_id/agent 匹配
        db.add(
            ChatSession(
                id="s_legacy",
                tenant_id="tenant_demo",
                user_id="u_legacy",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_p2p_ulegacy",
                updated_at=datetime(2026, 7, 18, 11, 0, 0),
            )
        )
        # legacy 但 agent 不在挂载集 → 排除
        db.add(
            ChatSession(
                id="s_legacy_other_agent",
                tenant_id="tenant_demo",
                user_id="u_legacy",
                agent_id="agent_other",
                channel="wechat",
                external_conv_id="wechat_p2p_other",
                updated_at=datetime(2026, 7, 18, 13, 0, 0),
            )
        )
        # 其他绑定的会话 → 排除
        db.add(
            ChatSession(
                id="s_other_binding",
                tenant_id="tenant_demo",
                user_id="u_wx1",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_p2p_u1",
                channel_binding_id="chan_someone_else",
                updated_at=datetime(2026, 7, 18, 14, 0, 0),
            )
        )
        # web 会话 → 排除
        db.add(ChatSession(id="s_web", tenant_id="tenant_demo", agent_id="agent_1"))
        db.add(
            Message(
                id="m1",
                tenant_id="tenant_demo",
                session_id="s_p2p",
                role="user",
                content="你好",
                created_at=datetime(2026, 7, 18, 9, 0, 0),
            )
        )
        db.add(
            Message(
                id="m2",
                tenant_id="tenant_demo",
                session_id="s_p2p",
                role="assistant",
                content="x" * 100,
                created_at=datetime(2026, 7, 18, 9, 0, 1),
            )
        )
        db.add(
            Message(
                id="m3",
                tenant_id="tenant_demo",
                session_id="s_group",
                role="user",
                content="群里第一句",
                created_at=datetime(2026, 7, 18, 11, 0, 0),
            )
        )
        db.commit()


def test_list_channel_conversations_shape_and_order() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    _seed_conversations(engine, binding_id)

    client = _make_client(engine)
    response = client.get(
        f"/api/enterprise/channels/{binding_id}/conversations?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    rows = payload["items"]
    # 按 updated_at 倒序:group(12:00) > legacy(11:00) > p2p(10:00)
    assert [row["session_id"] for row in rows] == ["s_group", "s_legacy", "s_p2p"]

    group = rows[0]
    assert group["is_group"] is True
    assert group["display_name"] == "微信群聊 room_1"
    assert group["agent_id"] == "agent_1"
    assert group["agent_name"] == "客服员工"
    assert group["message_count"] == 1
    assert group["last_message_preview"] == "群里第一句"

    legacy = rows[1]
    # 无 identity 时回退 User.display_name
    assert legacy["display_name"] == "老用户"
    assert legacy["is_group"] is False
    assert legacy["message_count"] == 0
    assert legacy["last_message_preview"] is None

    p2p = rows[2]
    # identity 的 display_name 优先于 User.display_name
    assert p2p["display_name"] == "微信用户 ab12cd34"
    assert p2p["message_count"] == 2
    assert p2p["last_message_preview"] == "x" * 60


def test_list_channel_conversations_excludes_other_bindings() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    _seed_conversations(engine, binding_id)

    client = _make_client(engine)
    response = client.get(
        f"/api/enterprise/channels/{binding_id}/conversations?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    session_ids = {row["session_id"] for row in response.json()["items"]}
    assert "s_other_binding" not in session_ids
    assert "s_legacy_other_agent" not in session_ids
    assert "s_web" not in session_ids


def test_list_channel_conversation_messages_order_and_404() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    _seed_conversations(engine, binding_id)
    with Session(engine) as db:
        message = db.get(Message, "m1")
        message.metadata_json = {
            "attachments": [
                {
                    "id": "file-1",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size": 123,
                    "kind": "image",
                    "data_url": "data:image/png;base64,SECRET",
                    "sandbox_path": "/workspace/attachments/internal.png",
                    "sha256": "a" * 64,
                    "text": "internal text",
                    "python_summary": "internal summary",
                }
            ]
        }
        db.add(message)
        db.commit()

    client = _make_client(engine)
    response = client.get(
        f"/api/enterprise/channels/{binding_id}/conversations/s_p2p/messages?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    rows = response.json()
    # created_at 升序
    assert [row["id"] for row in rows] == ["m1", "m2"]
    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "你好"
    assert rows[0]["created_at"]
    assert rows[0]["attachments"] == [
        {
            "id": "file-1",
            "filename": "image.png",
            "content_type": "image/png",
            "size": 123,
            "kind": "image",
        }
    ]
    assert rows[1]["attachments"] is None

    # 其他绑定的会话 → 404
    other = client.get(
        f"/api/enterprise/channels/{binding_id}/conversations/s_other_binding/messages?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert other.status_code == 404
    missing = client.get(
        f"/api/enterprise/channels/{binding_id}/conversations/session_nope/messages?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert missing.status_code == 404


def test_list_channel_conversation_messages_includes_attachments() -> None:
    """metadata_json 中存储的 attachments 应回填到响应里。"""
    from datetime import datetime

    from app.db.models import ChatSession, Message

    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    with Session(engine) as db:
        db.add(
            ChatSession(
                id="s_att",
                tenant_id="tenant_demo",
                user_id="u_wx1",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_p2p_u1",
                channel_binding_id=binding_id,
            )
        )
        db.add(
            Message(
                id="m_att",
                tenant_id="tenant_demo",
                session_id="s_att",
                role="user",
                content="看这张图",
                created_at=datetime(2026, 7, 18, 9, 0, 0),
                metadata_json={
                    "attachments": [
                        {
                            "id": "file_abc",
                            "filename": "photo.png",
                            "content_type": "image/png",
                            "size": 1024,
                            "kind": "image",
                            # 内部/大体量字段不应回传给渠道管理页面
                            "data_url": "data:image/png;base64,iVBORw0KGgo=",
                            "sandbox_path": "/sandbox/attachments/photo.png",
                            "sha256": "abc123",
                            "text": "secret",
                        }
                    ]
                },
            )
        )
        db.commit()

    client = _make_client(engine)
    response = client.get(
        f"/api/enterprise/channels/{binding_id}/conversations/s_att/messages?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["attachments"] == [
        {
            "id": "file_abc",
            "filename": "photo.png",
            "content_type": "image/png",
            "size": 1024,
            "kind": "image",
        }
    ]


def test_channel_conversations_require_manager() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    _seed_conversations(engine, binding_id)

    client = _make_client(engine)
    conversations = client.get(
        f"/api/enterprise/channels/{binding_id}/conversations?tenant_id=tenant_demo",
        headers=_auth(users["other"]),
    )
    assert conversations.status_code == 403
    messages = client.get(
        f"/api/enterprise/channels/{binding_id}/conversations/s_p2p/messages?tenant_id=tenant_demo",
        headers=_auth(users["other"]),
    )
    assert messages.status_code == 403


# ---------- PUT agents 可选化 ----------


def test_put_binding_auto_route_only() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)

    response = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"auto_route": False},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["auto_route"] is False
    # 挂载集未动(legacy 回退仍为默认员工)
    assert [(a["agent_id"], a["is_default"]) for a in payload["agents"]] == [("agent_1", True)]
    with Session(engine) as db:
        assert db.get(ChannelBinding, binding_id).config_json["auto_route"] is False


def test_put_binding_agents_only() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)

    response = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_1"}]},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert [(a["agent_id"], a["is_default"]) for a in payload["agents"]] == [("agent_1", True)]
    # auto_route 不传不动(默认 True)
    assert payload["auto_route"] is True
    with Session(engine) as db:
        assert "auto_route" not in (db.get(ChannelBinding, binding_id).config_json or {})


def test_put_binding_agents_and_auto_route_together() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)

    response = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_1"}], "auto_route": False},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["auto_route"] is False
    assert [(a["agent_id"], a["is_default"]) for a in payload["agents"]] == [("agent_1", True)]


def test_put_binding_empty_update_400() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)

    response = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 400


# ---------- 分页 ----------


def test_conversations_pagination() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    _seed_conversations(engine, binding_id)
    client = _make_client(engine)
    url = f"/api/enterprise/channels/{binding_id}/conversations"

    # 默认 offset=0 limit=20:全部 3 条,顺序不变
    default = client.get(url, params={"tenant_id": "tenant_demo"}, headers=_auth(users["owner"]))
    payload = default.json()
    assert payload["total"] == 3 and payload["offset"] == 0 and payload["limit"] == 20
    assert [row["session_id"] for row in payload["items"]] == ["s_group", "s_legacy", "s_p2p"]

    page1 = client.get(
        url, params={"tenant_id": "tenant_demo", "limit": 2}, headers=_auth(users["owner"])
    ).json()
    assert [row["session_id"] for row in page1["items"]] == ["s_group", "s_legacy"]
    assert page1["total"] == 3 and page1["limit"] == 2

    page2 = client.get(
        url, params={"tenant_id": "tenant_demo", "limit": 2, "offset": 2}, headers=_auth(users["owner"])
    ).json()
    assert [row["session_id"] for row in page2["items"]] == ["s_p2p"]
    assert page2["offset"] == 2 and page2["total"] == 3

    beyond = client.get(
        url, params={"tenant_id": "tenant_demo", "offset": 10}, headers=_auth(users["owner"])
    ).json()
    assert beyond["items"] == [] and beyond["total"] == 3

    too_large = client.get(
        url, params={"tenant_id": "tenant_demo", "limit": 101}, headers=_auth(users["owner"])
    )
    assert too_large.status_code == 422
    negative = client.get(
        url, params={"tenant_id": "tenant_demo", "offset": -1}, headers=_auth(users["owner"])
    )
    assert negative.status_code == 422


def test_deliveries_pagination() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine, status="active")
    from datetime import datetime

    with Session(engine) as db:
        for index in range(3):
            db.add(
                ChannelDelivery(
                    tenant_id="tenant_demo",
                    binding_id=binding_id,
                    session_id="session_1",
                    message_id=f"msg_{index}",
                    target_json={"to_user_id": "u1", "context_token": "ctx"},
                    kind="reply",
                    text=f"回复{index}",
                    status="delivered",
                    idempotency_key=f"msg_{index}",
                    created_at=datetime(2026, 7, 18, 10, 0, index),
                )
            )
        db.commit()

    client = _make_client(engine)
    url = f"/api/enterprise/channels/{binding_id}/deliveries"

    default = client.get(url, params={"tenant_id": "tenant_demo"}, headers=_auth(users["owner"]))
    payload = default.json()
    # created_at 倒序:msg_2 最新
    assert payload["total"] == 3 and payload["offset"] == 0 and payload["limit"] == 50
    assert [row["message_id"] for row in payload["items"]] == ["msg_2", "msg_1", "msg_0"]

    page1 = client.get(
        url, params={"tenant_id": "tenant_demo", "limit": 2}, headers=_auth(users["owner"])
    ).json()
    assert [row["message_id"] for row in page1["items"]] == ["msg_2", "msg_1"]
    assert page1["total"] == 3 and page1["limit"] == 2

    page2 = client.get(
        url, params={"tenant_id": "tenant_demo", "limit": 2, "offset": 2}, headers=_auth(users["owner"])
    ).json()
    assert [row["message_id"] for row in page2["items"]] == ["msg_0"]
    assert page2["offset"] == 2

    too_large = client.get(
        url, params={"tenant_id": "tenant_demo", "limit": 101}, headers=_auth(users["owner"])
    )
    assert too_large.status_code == 422

    forbidden = client.get(
        url, params={"tenant_id": "tenant_demo"}, headers=_auth(users["other"])
    )
    assert forbidden.status_code == 403


# ---------- 投递日志按天分组 ----------


def _seed_delivery_days(engine, binding_id: str) -> None:
    from datetime import datetime

    # 时间取正午附近,避免时区换算跨日影响断言
    seeds = [
        ("d1_a", datetime(2026, 7, 16, 10, 0, 0)),
        ("d1_b", datetime(2026, 7, 16, 12, 0, 0)),
        ("d2_a", datetime(2026, 7, 17, 11, 0, 0)),
        ("d3_a", datetime(2026, 7, 18, 10, 0, 0)),
        ("d3_b", datetime(2026, 7, 18, 12, 0, 0)),
        ("d3_c", datetime(2026, 7, 18, 13, 0, 0)),
    ]
    with Session(engine) as db:
        for key, created in seeds:
            db.add(
                ChannelDelivery(
                    tenant_id="tenant_demo",
                    binding_id=binding_id,
                    session_id="session_1",
                    message_id=f"msg_{key}",
                    target_json={"to_user_id": "u1", "context_token": "ctx"},
                    kind="reply",
                    text=f"回复{key}",
                    status="delivered",
                    idempotency_key=f"msg_{key}",
                    created_at=created,
                )
            )
        db.commit()


def test_deliveries_days_grouping_and_order() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine, status="active")
    _seed_delivery_days(engine, binding_id)

    client = _make_client(engine)
    response = client.get(
        f"/api/enterprise/channels/{binding_id}/deliveries/days?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_days"] == 3
    assert payload["offset"] == 0 and payload["limit"] == 7

    days = payload["days"]
    # 日期倒序
    assert [day["date"] for day in days] == ["2026-07-18", "2026-07-17", "2026-07-16"]
    assert [day["count"] for day in days] == [3, 1, 2]
    # 整天不截断:7-18 三条全在,组内 created_at 倒序
    assert [row["message_id"] for row in days[0]["items"]] == ["msg_d3_c", "msg_d3_b", "msg_d3_a"]
    assert [row["message_id"] for row in days[2]["items"]] == ["msg_d1_b", "msg_d1_a"]


def test_deliveries_days_pagination_boundaries() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine, status="active")
    _seed_delivery_days(engine, binding_id)

    client = _make_client(engine)
    url = f"/api/enterprise/channels/{binding_id}/deliveries/days"

    page1 = client.get(
        url, params={"tenant_id": "tenant_demo", "limit": 2}, headers=_auth(users["owner"])
    ).json()
    assert [day["date"] for day in page1["days"]] == ["2026-07-18", "2026-07-17"]
    assert page1["total_days"] == 3 and page1["limit"] == 2

    page2 = client.get(
        url,
        params={"tenant_id": "tenant_demo", "limit": 2, "offset": 2},
        headers=_auth(users["owner"]),
    ).json()
    assert [day["date"] for day in page2["days"]] == ["2026-07-16"]
    assert page2["offset"] == 2 and page2["total_days"] == 3

    beyond = client.get(
        url, params={"tenant_id": "tenant_demo", "offset": 10}, headers=_auth(users["owner"])
    ).json()
    assert beyond["days"] == [] and beyond["total_days"] == 3

    too_large = client.get(
        url, params={"tenant_id": "tenant_demo", "limit": 31}, headers=_auth(users["owner"])
    )
    assert too_large.status_code == 422
    negative = client.get(
        url, params={"tenant_id": "tenant_demo", "offset": -1}, headers=_auth(users["owner"])
    )
    assert negative.status_code == 422


def test_deliveries_days_requires_manager() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine, status="active")
    _seed_delivery_days(engine, binding_id)

    client = _make_client(engine)
    forbidden = client.get(
        f"/api/enterprise/channels/{binding_id}/deliveries/days?tenant_id=tenant_demo",
        headers=_auth(users["other"]),
    )
    assert forbidden.status_code == 403


# ---------- 绑定管理权限不随默认员工漂移 ----------


def test_binding_manager_not_drifted_by_default_agent() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    with Session(engine) as db:
        # agent_3 属于 other;绑定由 owner 创建、默认员工是 agent_3
        db.add(
            AgentProfile(
                id="agent_3",
                tenant_id="tenant_demo",
                name="他人的员工",
                metadata_json={"owner_user_id": users["other"].id},
            )
        )
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_3",
            channel="wechat",
            status="active",
            created_by_user_id=users["owner"].id,
        )
        db.add(binding)
        db.commit()
        binding_id = binding.id

    client = _make_client(engine)
    # other 拥有默认员工,但不是绑定创建者也不是 admin → 不能管
    assert client.get(
        f"/api/enterprise/channels/{binding_id}/agents?tenant_id=tenant_demo",
        headers=_auth(users["other"]),
    ).status_code == 403
    assert client.delete(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        headers=_auth(users["other"]),
    ).status_code == 403
    assert client.post(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode?tenant_id=tenant_demo",
        headers=_auth(users["other"]),
    ).status_code == 403

    # 创建者换默认员工后仍可管(PUT 换默认 → 再 PUT/DELETE 均放行)
    with Session(engine) as db:
        db.add(
            AgentProfile(
                id="agent_2",
                tenant_id="tenant_demo",
                name="财务员工",
                metadata_json={"owner_user_id": users["other"].id},
            )
        )
        db.commit()
    # agent_2 属于 other,owner 无管理权 → PUT 校验逐员工 manager 应 403(该校验不变)
    assert client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_3"}, {"agent_id": "agent_2"}]},
        headers=_auth(users["owner"]),
    ).status_code == 403

    # 创建者 PUT 换默认员工为自己拥有的 agent_1 → 放行(绑定权限看 created_by,不看默认员工归属)
    updated = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_1", "is_default": True}]},
        headers=_auth(users["owner"]),
    )
    assert updated.status_code == 200
    assert updated.json()["agent_id"] == "agent_1"

    # 换完默认员工后创建者仍可管
    assert client.get(
        f"/api/enterprise/channels/{binding_id}/agents?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    ).status_code == 200

    # admin 恒可
    assert client.delete(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        headers=_auth(users["admin"]),
    ).status_code == 204


# ---------- redirect_host / baseurl 域名校验 ----------


def test_scaned_but_redirect_rejects_untrusted_host(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)

    for bad_host in ("evil.com", "weixin.qq.com.evil.com"):
        _FakeWeChatClient.reset({"status": "scaned_but_redirect", "redirect_host": bad_host})
        monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)
        response = client.get(
            f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
            params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
            headers=_auth(users["owner"]),
        )
        assert response.status_code == 502, bad_host
        with Session(engine) as db:
            # 非法域名不存不用
            assert "qrcode_redirect_baseurl" not in (db.get(ChannelBinding, binding_id).config_json or {})


def test_scaned_but_redirect_accepts_official_subdomain(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)
    _FakeWeChatClient.reset(
        {"status": "scaned_but_redirect", "redirect_host": "szilinkai.weixin.qq.com"}
    )
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)

    response = client.get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    with Session(engine) as db:
        config = db.get(ChannelBinding, binding_id).config_json
        assert config["qrcode_redirect_baseurl"] == "https://szilinkai.weixin.qq.com"


def test_confirmed_sanitizes_baseurl(monkeypatch) -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    binding_id = _seed_binding(engine)
    client = _make_client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)

    _FakeWeChatClient.reset(
        {
            "status": "confirmed",
            "bot_token": "tok_x",
            "ilink_bot_id": "bot@im.bot",
            "baseurl": "https://evil.com/steal",
        }
    )
    monkeypatch.setattr(channels_api, "WeChatClient", _FakeWeChatClient)
    response = client.get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_1"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    with Session(engine) as db:
        # 非法 baseurl 不落库,回退官方默认
        assert db.get(ChannelBinding, binding_id).config_json["baseurl"] == (
            "https://ilinkai.weixin.qq.com"
        )

    _FakeWeChatClient.reset(
        {
            "status": "confirmed",
            "bot_token": "tok_y",
            "ilink_bot_id": "bot@im.bot",
            "baseurl": "https://szilinkai.weixin.qq.com/ilink/bot",
        }
    )
    response = client.get(
        f"/api/enterprise/channels/{binding_id}/wechat/qrcode-status",
        params={"tenant_id": "tenant_demo", "qrcode": "qrc_2"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    with Session(engine) as db:
        # 合法子域:规范化为 https://{host}
        assert db.get(ChannelBinding, binding_id).config_json["baseurl"] == (
            "https://szilinkai.weixin.qq.com"
        )


def test_post_binding_allows_multiple_bindings_per_agent_channel() -> None:
    engine = _test_engine()
    users = _seed_users(engine)
    client = _make_client(engine)

    first = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "agent_id": "agent_1", "channel": "wechat"},
        headers=_auth(users["owner"]),
    )
    second = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "agent_id": "agent_1", "channel": "wechat"},
        headers=_auth(users["owner"]),
    )
    assert first.status_code == 200 and second.status_code == 200
    # 同 Agent 同渠道:总是创建新绑定,不再命中返回旧实例
    assert first.json()["id"] != second.json()["id"]
