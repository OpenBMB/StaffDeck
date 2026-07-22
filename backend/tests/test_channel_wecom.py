import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.channels.service_intake as intake_module
import app.core.agent_loop as agent_loop_module
from app.channels.adapters.wecom import (
    WeComAdapter,
    WeComStreamManager,
    is_self_frame,
    normalize_wecom_frame,
)
from app.channels.service_identity import channel_username
from app.channels.service_intake import process_inbound
from app.channels.crypto import decrypt_channel_secret, encrypt_channel_secret
from app.db.models import (
    AgentProfile,
    ChannelBinding,
    ChannelInboundEvent,
    ChatSession,
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


def _text_frame(**overrides) -> dict:
    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "req_1"},
        "body": {
            "msgid": "msg_1",
            "aibotid": "aib_bot1",
            "chattype": "single",
            "from": {"userid": "zhangsan"},
            "msgtype": "text",
            "text": {"content": "你好"},
        },
    }
    body = frame["body"]
    for key, value in overrides.items():
        if key == "headers":
            frame["headers"] = value
        else:
            body[key] = value
    return frame


def _seed_wecom_binding(engine, **overrides) -> str:
    with Session(engine) as db:
        if not db.get(Tenant, "tenant_demo"):
            db.add(Tenant(id="tenant_demo", name="Demo"))
        values = {
            "tenant_id": "tenant_demo",
            "agent_id": "agent_1",
            "channel": "wecom",
            "status": "active",
            "credentials_enc": encrypt_channel_secret("bot_secret"),
            "config_json": {"bot_id": "aib_bot1"},
            "created_by_user_id": "user_owner",
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


# ---------- 帧归一化 ----------


def test_normalize_text_frame() -> None:
    inbound = normalize_wecom_frame(_text_frame())
    assert inbound is not None
    assert inbound.channel == "wecom"
    assert inbound.event_id == "msg_1"
    assert inbound.from_user_id == "zhangsan"
    assert inbound.text == "你好"
    assert inbound.is_group is False
    assert inbound.external_conv_id == "wecom_p2p_zhangsan"
    assert inbound.context_token == "zhangsan"


def test_normalize_voice_frame_uses_transcript() -> None:
    frame = _text_frame(msgtype="voice", text=None, voice={"content": "我下午三点到"})
    inbound = normalize_wecom_frame(frame)
    assert inbound is not None
    assert inbound.text == "我下午三点到"


def test_normalize_group_frame() -> None:
    frame = _text_frame(
        chatid="wrQoP7CwAAA",
        chattype="group",
        **{"from": {"userid": "zhangsan", "name": "张三"}},
    )
    inbound = normalize_wecom_frame(frame)
    assert inbound is not None
    assert inbound.is_group is True
    assert inbound.conv_key == "wrQoP7CwAAA"
    assert inbound.external_conv_id == "wecom_group_wrQoP7CwAAA"
    assert inbound.context_token == "wrQoP7CwAAA"
    assert inbound.sender_name == "张三"


def test_normalize_drops_self_and_invalid_frames() -> None:
    self_frame = _text_frame(**{"from": {"userid": "aib_bot1"}})
    assert is_self_frame(self_frame) is True
    assert normalize_wecom_frame(self_frame) is None
    # 图片消息(本期不支持)
    image_frame = _text_frame(msgtype="image", text=None, image={"url": "x"})
    assert normalize_wecom_frame(image_frame) is None
    # 缺 msgid/req_id
    no_id = _text_frame(msgid=None, headers={})
    assert normalize_wecom_frame(no_id) is None
    # 缺发送者
    no_sender = _text_frame(**{"from": {}})
    assert normalize_wecom_frame(no_sender) is None


def test_event_id_falls_back_to_req_id() -> None:
    frame = _text_frame(msgid=None, headers={"req_id": "req_fallback"})
    inbound = normalize_wecom_frame(frame)
    assert inbound is not None and inbound.event_id == "req_fallback"


# ---------- send ----------


class _FakeStreamClient:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send_message(self, chatid: str, body: dict):
        self.sent.append((chatid, body))
        return {}


def _run_loop_in_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def test_send_via_run_coroutine_threadsafe_and_split(monkeypatch) -> None:
    import app.channels

    client = _FakeStreamClient()
    loop, _thread = _run_loop_in_thread()
    fake_manager = SimpleNamespace(get_stream=lambda binding_id: (client, loop))
    monkeypatch.setattr(app.channels, "get_wecom_stream_manager", lambda: fake_manager)
    try:
        adapter = WeComAdapter()
        binding = ChannelBinding(tenant_id="t", agent_id="a", channel="wecom", status="active")
        adapter.send(binding, {"to_user_id": "chat_1", "context_token": "chat_1"}, "x" * 4500)
    finally:
        loop.call_soon_threadsafe(loop.stop)

    assert len(client.sent) == 3
    assert all(chatid == "chat_1" for chatid, _ in client.sent)
    texts = [body["markdown"]["content"] for _, body in client.sent]
    assert "".join(texts) == "x" * 4500
    assert all(len(chunk) <= 2000 for chunk in texts)
    assert all(body["msgtype"] == "markdown" for _, body in client.sent)


def test_send_raises_when_stream_not_ready(monkeypatch) -> None:
    import app.channels

    fake_manager = SimpleNamespace(get_stream=lambda binding_id: None)
    monkeypatch.setattr(app.channels, "get_wecom_stream_manager", lambda: fake_manager)
    adapter = WeComAdapter()
    binding = ChannelBinding(tenant_id="t", agent_id="a", channel="wecom", status="active")
    with pytest.raises(RuntimeError):
        adapter.send(binding, {"to_user_id": "chat_1"}, "hi")


# ---------- StreamManager 生命周期 ----------


class FakeWSClient:
    def __init__(self):
        self.handlers: dict = {}
        self.connect_calls = 0
        self.disconnect_calls = 0

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    async def connect(self):
        self.connect_calls += 1
        return self

    def disconnect(self):
        self.disconnect_calls += 1

    def emit_local(self, event: str, *args) -> None:
        handler = self.handlers.get(event)
        if handler:
            handler(*args)


def _wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_stream_manager_lifecycle_and_connected_state() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    fake = FakeWSClient()
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: fake)

    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: fake.connect_calls == 1)
    assert _wait_for(lambda: manager.get_stream(binding_id) is not None)

    fake.emit_local("authenticated")
    assert _wait_for(lambda: _load_binding(engine, binding_id).connected is True)

    fake.emit_local("disconnected", "lost")
    assert _wait_for(lambda: _load_binding(engine, binding_id).connected is False)

    manager.stop_binding(binding_id)
    assert _wait_for(lambda: fake.disconnect_calls == 1)
    assert _wait_for(lambda: manager.get_stream(binding_id) is None)


def test_stream_manager_ingress_requires_credentials() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine, credentials_enc=None, config_json={})
    fake = FakeWSClient()
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: fake)

    manager.ensure_binding(binding_id)
    time.sleep(0.3)
    # 缺凭证:线程直接退出,不创建 client
    assert fake.connect_calls == 0
    assert manager.get_stream(binding_id) is None


def test_stream_manager_reconcile_hot_start_stop() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    fake = FakeWSClient()
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: fake)

    manager.reconcile_once()
    assert binding_id in manager.running_binding_ids() or _wait_for(
        lambda: binding_id in manager.running_binding_ids()
    )
    assert _wait_for(lambda: fake.connect_calls == 1)

    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.status = "disabled"
        db.add(binding)
        db.commit()
    manager.reconcile_once()
    assert _wait_for(lambda: fake.disconnect_calls == 1)


def test_stream_manager_dispatches_inbound_to_intake(monkeypatch) -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    processed: list = []

    def fake_process_inbound(binding, inbound, *, db_engine=None):
        processed.append(inbound)
        return True

    monkeypatch.setattr(intake_module, "process_inbound", fake_process_inbound)
    fake = FakeWSClient()
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: fake)
    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: fake.connect_calls == 1)

    fake.emit_local("message", _text_frame())
    assert _wait_for(lambda: len(processed) == 1)
    assert processed[0].channel == "wecom"
    manager.stop_binding(binding_id)


def test_frame_handler_returns_immediately_without_blocking(monkeypatch) -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    processed: list = []

    def slow_process_inbound(binding, inbound, *, db_engine=None):
        time.sleep(1.0)
        processed.append(inbound)
        return True

    monkeypatch.setattr(intake_module, "process_inbound", slow_process_inbound)
    fake = FakeWSClient()
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: fake)
    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: fake.connect_calls == 1)

    started = time.monotonic()
    fake.emit_local("message", _text_frame())
    elapsed = time.monotonic() - started
    # handler 只入队:loop 线程不被 process_inbound 阻塞,立即返回继续心跳
    assert elapsed < 0.5
    # 消息随后仍在 worker 线程被完整处理
    assert _wait_for(lambda: len(processed) == 1, timeout=5.0)
    manager.stop_binding(binding_id)


def test_inbound_messages_are_processed_in_order(monkeypatch) -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    processed: list[str] = []
    first_started = threading.Event()

    def ordered_process_inbound(binding, inbound, *, db_engine=None):
        if inbound.event_id == "msg_first":
            first_started.set()
            time.sleep(0.3)
        processed.append(inbound.event_id)
        return True

    monkeypatch.setattr(intake_module, "process_inbound", ordered_process_inbound)
    fake = FakeWSClient()
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: fake)
    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: fake.connect_calls == 1)

    fake.emit_local("message", _text_frame(msgid="msg_first"))
    assert first_started.wait(timeout=5.0)
    # 第一条还在处理时投入第二条:单 worker 保证顺序
    fake.emit_local("message", _text_frame(msgid="msg_second"))
    assert _wait_for(lambda: len(processed) == 2, timeout=5.0)
    assert processed == ["msg_first", "msg_second"]
    manager.stop_binding(binding_id)


def test_worker_exits_on_stop_binding() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    fake = FakeWSClient()
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: fake)
    manager.ensure_binding(binding_id)
    with manager._lock:
        worker = manager._streams[binding_id].worker
    assert worker and worker.is_alive()

    manager.stop_binding(binding_id)
    assert not worker.is_alive()


# ---------- intake 集成(企微会话/身份/路由) ----------


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


def test_wecom_p2p_inbound_full_chain() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    binding = _load_binding(engine, binding_id)

    inbound = normalize_wecom_frame(_text_frame(msgid="msg_p1"))
    assert process_inbound(binding, inbound, db_engine=engine) is True
    assert len(RecordingAgentLoop.calls) == 1
    request = RecordingAgentLoop.calls[0]
    assert request.channel == "wecom"
    assert request.client_turn_id == "msg_p1"

    with Session(engine) as db:
        chat_session = db.get(ChatSession, request.session_id)
        assert chat_session.external_conv_id == "wecom_aib_bot1_p2p_zhangsan"
        assert chat_session.channel_binding_id == binding_id
        user = db.get(User, chat_session.user_id)
        assert user.username == channel_username("tenant_demo", "wecom", "zhangsan", "aib_bot1")
        assert user.display_name == "企微用户 zhangsan"
        assert user.source == "wecom"
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "done"


def test_wecom_group_inbound_uses_sender_name_prefix() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    binding = _load_binding(engine, binding_id)

    frame = _text_frame(
        msgid="msg_g1",
        chatid="wr_group1",
        chattype="group",
        **{"from": {"userid": "zhangsan", "name": "张三"}},
    )
    inbound = normalize_wecom_frame(frame)
    assert process_inbound(binding, inbound, db_engine=engine) is True
    request = RecordingAgentLoop.calls[0]
    assert request.message.startswith("[发送者: 张三]\n")

    with Session(engine) as db:
        chat_session = db.get(ChatSession, request.session_id)
        assert chat_session.external_conv_id == "wecom_aib_bot1_group_wr_group1"
        # 群聊回复投递到群 chatid
        assert chat_session.channel_target_json["to_user_id"] == "wr_group1"
        group_user = db.get(User, chat_session.user_id)
        assert group_user.username == channel_username("tenant_demo", "wecom", "group_aib_bot1_wr_group1")


def test_wecom_switch_command_routes_agents() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(AgentProfile(id="agent_1", tenant_id="tenant_demo", name="行政", metadata_json={}))
        db.add(AgentProfile(id="agent_2", tenant_id="tenant_demo", name="财务", metadata_json={}))
        db.commit()
    binding_id = _seed_wecom_binding(engine)
    with Session(engine) as db:
        from app.db.models import ChannelBindingAgent

        db.add(
            ChannelBindingAgent(
                tenant_id="tenant_demo", binding_id=binding_id, agent_id="agent_1", is_default=True
            )
        )
        db.add(ChannelBindingAgent(tenant_id="tenant_demo", binding_id=binding_id, agent_id="agent_2"))
        db.commit()
    binding = _load_binding(engine, binding_id)

    cmd = normalize_wecom_frame(_text_frame(msgid="msg_c1", text={"content": "/切换 财务"}))
    assert process_inbound(binding, cmd, db_engine=engine) is False
    msg = normalize_wecom_frame(_text_frame(msgid="msg_c2", text={"content": "报销流程?"}))
    assert process_inbound(binding, msg, db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_2"


# ---------- API ----------


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
        db.add(
            AgentProfile(
                id="agent_1",
                tenant_id="tenant_demo",
                name="客服员工",
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


def test_wecom_credentials_endpoint(monkeypatch) -> None:
    import app.api.channels as channels_api

    engine = _test_engine()
    users = _seed_api_users(engine)
    binding_id = _seed_wecom_binding(engine, status="pending", credentials_enc=None, config_json={})
    client = _make_api_client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)

    forbidden = client.post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "bot_secret"},
        headers=_auth(users["other"]),
    )
    assert forbidden.status_code == 403

    response = client.post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "bot_secret"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "active"
    assert payload["bot_id"] == "aib_bot1"
    assert payload["channel"] == "wecom"
    assert "bot_secret" not in response.text

    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.status == "active"
        assert decrypt_channel_secret(binding.credentials_enc) == "bot_secret"
        assert binding.config_json["bot_id"] == "aib_bot1"


def test_wecom_credentials_rejects_wechat_binding() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    with Session(engine) as db:
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wechat",
            status="pending",
            created_by_user_id="user_owner",
        )
        db.add(binding)
        db.commit()
        binding_id = binding.id

    client = _make_api_client(engine)
    response = client.post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "x"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 400


def test_create_wecom_binding_supported() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    client = _make_api_client(engine)

    response = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_demo", "agent_id": "agent_1", "channel": "wecom"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    assert response.json()["channel"] == "wecom"


def test_channel_meta_endpoint() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    client = _make_api_client(engine)

    response = client.get(
        "/api/enterprise/channels/meta?tenant_id=tenant_demo",
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    rows = {item["channel"]: item for item in response.json()}
    assert rows["wechat"]["name"] == "微信"
    assert rows["wechat"]["setup"] == "qrcode"
    assert "typing" in rows["wechat"]["capabilities"]
    wecom = rows["wecom"]
    assert wecom["setup"] == "credentials"
    fields = {field["key"]: field for field in wecom["credential_fields"]}
    assert fields["bot_id"]["secret"] is False
    assert fields["secret"]["secret"] is True


# ---------- connected 状态对账 ----------


class _ConnectedFakeClient(FakeWSClient):
    def __init__(self, is_connected: bool):
        super().__init__()
        self.is_connected = is_connected


def test_reconcile_aligns_connected_from_sdk_truth() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)  # connected 默认 False
    fake = _ConnectedFakeClient(is_connected=True)
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: fake)
    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: fake.connect_calls == 1)

    manager.reconcile_once()
    assert _load_binding(engine, binding_id).connected is True

    # 实况翻转后,下一轮对账回 False
    fake.is_connected = False
    manager.reconcile_once()
    assert _load_binding(engine, binding_id).connected is False
    manager.stop_binding(binding_id)


def test_reconcile_marks_not_running_binding_disconnected() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine, connected=True)
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: FakeWSClient())
    # 不真正拉起线程,直接验证未运行分支
    manager.ensure_binding = lambda binding_id: None

    manager.reconcile_once()
    assert _load_binding(engine, binding_id).connected is False


def test_reconcile_skips_write_when_connected_unchanged(monkeypatch) -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine, connected=True)
    fake = _ConnectedFakeClient(is_connected=True)
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: fake)
    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: fake.connect_calls == 1)

    commits = {"count": 0}
    original_commit = Session.commit

    def counting_commit(self):
        commits["count"] += 1
        return original_commit(self)

    monkeypatch.setattr(Session, "commit", counting_commit)
    manager.reconcile_once()
    # DB 与 SDK 实况一致:_set_connected 内部无变化不写库
    assert commits["count"] == 0
    assert _load_binding(engine, binding_id).connected is True
    manager.stop_binding(binding_id)


# ---------- 重配凭证真正重启 ingress ----------


def test_wecom_credentials_change_restarts_stream_with_new_client() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine, bot_id="bot_1")
    created: list[tuple[str, str, FakeWSClient]] = []

    def factory(bot_id: str, secret: str):
        client = FakeWSClient()
        created.append((bot_id, secret, client))
        return client

    manager = WeComStreamManager(db_engine=engine, client_factory=factory)
    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: len(created) == 1)
    assert _wait_for(lambda: manager.get_stream(binding_id) is not None)
    first = created[0][2]

    # 换凭证:stop+wait 后旧 stream 退出,新 client 以新凭证接管
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.credentials_enc = encrypt_channel_secret("secret_2")
        binding.config_json = {"bot_id": "bot_2"}
        db.add(binding)
        db.commit()

    manager.stop_binding(binding_id)
    assert manager.wait_binding_stopped(binding_id, timeout_seconds=5.0) is True
    assert first.disconnect_calls == 1

    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: len(created) == 2)
    assert created[1][0] == "bot_2"
    assert created[1][1] == "secret_2"
    assert created[1][2] is not first


def test_wecom_endpoint_restart_flow_via_spy_manager(monkeypatch) -> None:
    import app.channels
    import app.api.channels as channels_api

    engine = _test_engine()
    users = _seed_api_users(engine)
    binding_id = _seed_wecom_binding(engine, status="pending", credentials_enc=None, config_json={})
    calls: list[str] = []

    class SpyManager:
        def stop_binding(self, bid):
            calls.append(f"stop:{bid}")

        def wait_binding_stopped(self, bid, timeout_seconds=5.0):
            calls.append(f"wait:{bid}")
            return True

        def ensure_binding(self, bid):
            calls.append(f"ensure:{bid}")

    monkeypatch.setattr(app.channels, "get_wecom_stream_manager", lambda: SpyManager())
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: True)

    client = _make_api_client(engine)
    response = client.post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={"tenant_id": "tenant_demo", "bot_id": "aib_bot1", "secret": "bot_secret"},
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    # stop → wait → start 顺序
    assert calls == [f"stop:{binding_id}", f"wait:{binding_id}", f"ensure:{binding_id}"]
