import asyncio
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
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
    ChannelIdentity,
    ChannelInboundEvent,
    ChatSession,
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
    assert manager.wait_binding_stopped(binding_id, timeout_seconds=5.0)
    assert not worker.is_alive()


def test_worker_sentinel_waits_for_producer_barrier() -> None:
    from app.channels.adapters.wecom import _StreamState

    class StubThread:
        def __init__(self, alive: bool):
            self.alive = alive

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            return None

    engine = _test_engine()
    manager = WeComStreamManager(db_engine=engine)
    state = _StreamState()
    producer = StubThread(alive=True)
    state.thread = producer
    with manager._lock:
        manager._streams["chan_barrier"] = state

    manager.stop_binding("chan_barrier")
    assert state.queue.empty()
    assert manager.wait_binding_stopped("chan_barrier", timeout_seconds=0.0) is False
    assert state.queue.empty()

    producer.alive = False
    assert manager.wait_binding_stopped("chan_barrier", timeout_seconds=0.0) is True
    assert state.queue.get_nowait() is None


def test_stop_waits_for_inflight_callback_before_worker_sentinel(monkeypatch) -> None:
    import app.channels.adapters.wecom as wecom_module
    from app.channels.adapters.wecom import _StreamState

    engine = _test_engine()
    manager = WeComStreamManager(db_engine=engine)
    state = _StreamState()
    state.config_revision = 3
    client = FakeWSClient()
    callback_entered = threading.Event()
    release_callback = threading.Event()
    wait_finished = threading.Event()
    inbound = object()

    def blocking_normalize(frame, account_scope=""):
        callback_entered.set()
        assert release_callback.wait(timeout=5.0)
        return inbound

    monkeypatch.setattr(wecom_module, "normalize_wecom_frame", blocking_normalize)
    manager._wire_client("chan_callback", client, state, "corpA")
    with manager._lock:
        manager._streams["chan_callback"] = state

    callback_thread = threading.Thread(
        target=client.emit_local,
        args=("message", _text_frame(msgid="msg_callback")),
    )
    callback_thread.start()
    assert callback_entered.wait(timeout=5.0)
    manager.stop_binding("chan_callback")

    wait_thread = threading.Thread(
        target=lambda: (
            manager.wait_binding_stopped("chan_callback", timeout_seconds=5.0),
            wait_finished.set(),
        )
    )
    wait_thread.start()
    assert not wait_finished.wait(timeout=0.1)
    release_callback.set()
    callback_thread.join(timeout=5.0)
    wait_thread.join(timeout=5.0)

    assert not callback_thread.is_alive()
    assert not wait_thread.is_alive()
    queued_revision, queued_inbound = state.queue.get_nowait()
    assert queued_revision == 3
    assert queued_inbound is inbound
    assert state.queue.get_nowait() is None


def test_stream_finally_waits_for_external_callback_and_worker_consumes_it(monkeypatch) -> None:
    import app.channels.adapters.wecom as wecom_module

    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    callback_entered = threading.Event()
    release_callback = threading.Event()
    processed = threading.Event()
    callback_threads: list[threading.Thread] = []
    inbound = SimpleNamespace(event_id="msg_external_callback")

    class ExternalCallbackClient(FakeWSClient):
        async def connect(self):
            self.connect_calls += 1
            thread = threading.Thread(
                target=self.emit_local,
                args=("message", _text_frame(msgid="msg_external_callback")),
            )
            callback_threads.append(thread)
            thread.start()
            return self

    def blocking_normalize(frame, account_scope=""):
        callback_entered.set()
        assert release_callback.wait(timeout=5.0)
        return inbound

    def record_process(binding, item, db_engine=None):
        assert item is inbound
        processed.set()
        return True

    client = ExternalCallbackClient()
    monkeypatch.setattr(wecom_module, "normalize_wecom_frame", blocking_normalize)
    monkeypatch.setattr(intake_module, "process_inbound", record_process)
    manager = WeComStreamManager(
        db_engine=engine,
        client_factory=lambda bot_id, secret: client,
    )
    manager.ensure_binding(binding_id)
    assert callback_entered.wait(timeout=5.0)

    manager.stop_binding(binding_id)
    wait_result: list[bool] = []
    wait_thread = threading.Thread(
        target=lambda: wait_result.append(
            manager.wait_binding_stopped(binding_id, timeout_seconds=5.0)
        )
    )
    wait_thread.start()
    time.sleep(0.05)
    assert wait_thread.is_alive()
    release_callback.set()
    wait_thread.join(timeout=5.0)
    for thread in callback_threads:
        thread.join(timeout=5.0)

    assert wait_result == [True]
    assert processed.is_set()
    assert all(not thread.is_alive() for thread in callback_threads)


def test_unexpected_stream_exit_rejects_late_callback() -> None:
    import app.channels.adapters.wecom as wecom_module

    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)

    class FailingClient(FakeWSClient):
        async def connect(self):
            raise RuntimeError("stream failed")

    client = FailingClient()
    manager = WeComStreamManager(
        db_engine=engine,
        client_factory=lambda bot_id, secret: client,
    )
    state = wecom_module._StreamState()
    state.config_revision = 0

    manager._run_stream(binding_id, state)
    assert state.stop.is_set()
    client.emit_local("message", _text_frame(msgid="late_callback"))

    assert state.queue.get_nowait() is None
    assert state.queue.empty()


def test_reconcile_does_not_stop_stream_while_revision_initializes(monkeypatch) -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.config_revision = 7
        db.add(binding)
        db.commit()

    release_stream = threading.Event()
    manager = WeComStreamManager(db_engine=engine)
    monkeypatch.setattr(
        manager,
        "_run_stream",
        lambda bid, state: release_stream.wait(timeout=5.0),
    )
    manager.ensure_binding(binding_id)
    with manager._lock:
        state = manager._streams[binding_id]
    assert state.config_revision == 7

    manager.reconcile_once()

    assert state.stop.is_set() is False
    release_stream.set()
    manager.stop_binding(binding_id)
    assert manager.wait_binding_stopped(binding_id, timeout_seconds=5.0)


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
        assert group_user.username == channel_username(
            "tenant_demo", "wecom", "group:wr_group1", "aib_bot1"
        )


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


def _make_api_client(engine, observed_sessions: list[Session] | None = None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.channels as channels_api
    from app.db import get_session

    app = FastAPI()
    app.include_router(channels_api.router)

    def override_get_session():
        with Session(engine) as session:
            if observed_sessions is not None:
                observed_sessions.append(session)
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
        json={
            "tenant_id": "tenant_demo",
            "bot_id": "aib_bot1",
            "secret": "bot_secret",
            "corp_id": "corpA",
        },
        headers=_auth(users["other"]),
    )
    assert forbidden.status_code == 403

    response = client.post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={
            "tenant_id": "tenant_demo",
            "bot_id": "aib_bot1",
            "secret": "bot_secret",
            "corp_id": "corpA",
        },
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
        assert binding.config_json["corp_id"] == "corpA"


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
        json={
            "tenant_id": "tenant_demo",
            "bot_id": "aib_bot1",
            "secret": "x",
            "corp_id": "corpA",
        },
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
    observed_sessions: list[Session] = []

    class SpyManager:
        def stop_binding(self, bid):
            assert observed_sessions
            assert observed_sessions[-1].in_transaction() is False
            calls.append(f"stop:{bid}")

        def wait_binding_stopped(self, bid, timeout_seconds=5.0):
            calls.append(f"wait:{bid}")
            return True

        def ensure_binding(self, bid):
            calls.append(f"ensure:{bid}")

    monkeypatch.setattr(app.channels, "get_wecom_stream_manager", lambda: SpyManager())
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: True)

    client = _make_api_client(engine, observed_sessions)
    response = client.post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={
            "tenant_id": "tenant_demo",
            "bot_id": "aib_bot1",
            "secret": "bot_secret",
            "corp_id": "corpA",
        },
        headers=_auth(users["owner"]),
    )
    assert response.status_code == 200
    # stop → wait → start 顺序
    assert calls == [f"stop:{binding_id}", f"wait:{binding_id}", f"ensure:{binding_id}"]


def test_wecom_credentials_rejects_bot_change_without_stopping_ingress(monkeypatch) -> None:
    import app.channels
    import app.api.channels as channels_api

    engine = _test_engine()
    users = _seed_api_users(engine)
    binding_id = _seed_wecom_binding(
        engine,
        config_json={"bot_id": "bot_old", "corp_id": "corpA"},
        external_account_key="wecom:corp:5:corpA:bot:7:bot_old",
        identity_scope_key="corpA",
    )
    calls: list[str] = []

    class SpyManager:
        def pause_binding(self, bid):
            calls.append(f"pause:{bid}")

    monkeypatch.setattr(app.channels, "get_wecom_stream_manager", lambda: SpyManager())
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: True)

    response = _make_api_client(engine).post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={
            "tenant_id": "tenant_demo",
            "bot_id": "bot_new",
            "secret": "new_secret",
            "corp_id": "corpA",
        },
        headers=_auth(users["owner"]),
    )

    assert response.status_code == 400
    assert "删除后重新创建绑定" in response.json()["detail"]
    assert calls == []
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.config_json["bot_id"] == "bot_old"
        assert decrypt_channel_secret(binding.credentials_enc) == "bot_secret"
        assert binding.config_revision == 0


def test_wecom_reconfigure_timeout_keeps_old_config_and_does_not_start(monkeypatch) -> None:
    import app.channels
    import app.api.channels as channels_api

    engine = _test_engine()
    users = _seed_api_users(engine)
    binding_id = _seed_wecom_binding(
        engine,
        config_json={"bot_id": "aib_bot1", "corp_id": "corpA"},
        external_account_key="wecom:corp:5:corpA:bot:8:aib_bot1",
        identity_scope_key="corpA",
    )
    calls: list[str] = []

    class SpyManager:
        def pause_binding(self, bid):
            calls.append(f"pause:{bid}")

        def wait_binding_stopped(self, bid, timeout_seconds=5.0):
            calls.append(f"wait:{bid}")
            return False

        def resume_binding(self, bid, *, start=True):
            calls.append(f"resume:{bid}:{start}")

    monkeypatch.setattr(app.channels, "get_wecom_stream_manager", lambda: SpyManager())
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: True)

    response = _make_api_client(engine).post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={
            "tenant_id": "tenant_demo",
            "bot_id": "aib_bot1",
            "secret": "new_secret",
            "corp_id": "corpA",
        },
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
        assert decrypt_channel_secret(binding.credentials_enc) == "bot_secret"
        assert binding.config_revision == 0


def test_wecom_reconfigure_commit_failure_restores_old_ingress(monkeypatch) -> None:
    import app.channels
    import app.api.channels as channels_api

    engine = _test_engine()
    users = _seed_api_users(engine)
    binding_id = _seed_wecom_binding(
        engine,
        config_json={"bot_id": "aib_bot1", "corp_id": "corpA"},
        external_account_key="wecom:corp:5:corpA:bot:8:aib_bot1",
        identity_scope_key="corpA",
    )
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

    monkeypatch.setattr(app.channels, "get_wecom_stream_manager", lambda: SpyManager())
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: True)
    monkeypatch.setattr(Session, "commit", fail_first_commit)

    response = _make_api_client(engine).post(
        f"/api/enterprise/channels/{binding_id}/wecom/credentials",
        json={
            "tenant_id": "tenant_demo",
            "bot_id": "aib_bot1",
            "secret": "new_secret",
            "corp_id": "corpA",
        },
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
        assert decrypt_channel_secret(binding.credentials_enc) == "bot_secret"
        assert binding.config_revision == 0


def test_wecom_reconcile_does_not_restart_paused_binding() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    manager = WeComStreamManager(
        db_engine=engine,
        client_factory=lambda bot_id, secret: FakeWSClient(),
    )
    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: binding_id in manager.running_binding_ids())

    manager.pause_binding(binding_id)
    assert manager.wait_binding_stopped(binding_id, timeout_seconds=5.0)
    manager.reconcile_once()

    assert binding_id not in manager.running_binding_ids()


def test_wecom_timeout_then_reconcile_restores_old_config_once(monkeypatch) -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    created: list[tuple[str, str, FakeWSClient]] = []
    turn_started = threading.Event()
    release_turn = threading.Event()

    def factory(bot_id: str, secret: str):
        client = FakeWSClient()
        created.append((bot_id, secret, client))
        return client

    def blocking_process(binding, inbound, db_engine=None):
        turn_started.set()
        assert release_turn.wait(timeout=5.0)
        return True

    monkeypatch.setattr(intake_module, "process_inbound", blocking_process)
    manager = WeComStreamManager(db_engine=engine, client_factory=factory)
    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: manager.get_stream(binding_id) is not None)
    created[0][2].emit_local("message", _text_frame(msgid="msg_slow"))
    assert turn_started.wait(timeout=5.0)

    manager.pause_binding(binding_id)
    assert manager.wait_binding_stopped(binding_id, timeout_seconds=0.05) is False
    manager.resume_binding(binding_id, start=False)
    manager.reconcile_once()
    assert len(created) == 1

    release_turn.set()
    assert manager.wait_binding_stopped(binding_id, timeout_seconds=5.0)
    manager.reconcile_once()
    assert _wait_for(lambda: len(created) == 2 and manager.get_stream(binding_id) is not None)
    assert created[1][0:2] == ("aib_bot1", "bot_secret")
    with manager._lock:
        assert len(manager._streams) == 1
    manager.pause_binding(binding_id)
    assert manager.wait_binding_stopped(binding_id, timeout_seconds=5.0)


def test_stale_wecom_callback_cannot_overwrite_new_revision_connected_state() -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        binding.config_revision = 2
        binding.connected = True
        db.add(binding)
        db.commit()

    manager = WeComStreamManager(db_engine=engine)
    manager._set_connected(binding_id, False, config_revision=1)

    with Session(engine) as db:
        assert db.get(ChannelBinding, binding_id).connected is True


def test_concurrent_wecom_secret_rotations_serialize_and_match_running_config(
    monkeypatch, tmp_path
) -> None:
    import app.channels
    import app.api.channels as channels_api

    db_path = tmp_path / "wecom-reconfigure.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    users = _seed_api_users(engine)
    binding_id = _seed_wecom_binding(
        engine,
        config_json={"bot_id": "aib_bot1", "corp_id": "corpA"},
        external_account_key="wecom:corp:5:corpA:bot:8:aib_bot1",
        identity_scope_key="corpA",
    )
    created: list[tuple[str, str, FakeWSClient]] = []

    def factory(bot_id: str, secret: str):
        client = FakeWSClient()
        created.append((bot_id, secret, client))
        return client

    manager = WeComStreamManager(db_engine=engine, client_factory=factory)
    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: manager.get_stream(binding_id) is not None)
    monkeypatch.setattr(app.channels, "get_wecom_stream_manager", lambda: manager)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: True)
    responses: list[tuple[int, str]] = []

    def rotate(secret: str) -> None:
        response = _make_api_client(engine).post(
            f"/api/enterprise/channels/{binding_id}/wecom/credentials",
            json={
                "tenant_id": "tenant_demo",
                "bot_id": "aib_bot1",
                "secret": secret,
                "corp_id": "corpA",
            },
            headers=_auth(users["owner"]),
        )
        responses.append((response.status_code, response.text))

    threads = [threading.Thread(target=rotate, args=(secret,)) for secret in ("s1", "s2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(status for status, _body in responses) == [200, 200], responses
    assert _wait_for(lambda: len(created) == 3 and manager.get_stream(binding_id) is not None)
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.config_revision == 2
        final_secret = decrypt_channel_secret(binding.credentials_enc)
    assert created[-1][1] == final_secret
    assert created[0][2].disconnect_calls == 1
    assert created[1][2].disconnect_calls in {0, 1}
    with manager._lock:
        state = manager._streams[binding_id]
        assert state.config_revision == 2
        assert state.thread and state.thread.is_alive()
        assert state.worker and state.worker.is_alive()
    manager.pause_binding(binding_id)
    assert manager.wait_binding_stopped(binding_id, timeout_seconds=5.0)


def test_delete_serializes_against_concurrent_credentials_update(monkeypatch, tmp_path) -> None:
    import app.channels
    import app.api.channels as channels_api

    db_path = tmp_path / "delete-race.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    users = _seed_api_users(engine)
    binding_id = _seed_wecom_binding(
        engine,
        config_json={"bot_id": "aib_bot1", "corp_id": "corpA"},
        external_account_key="wecom:corp:5:corpA:bot:8:aib_bot1",
        identity_scope_key="corpA",
    )
    delete_waiting = threading.Event()
    release_delete = threading.Event()
    starts: list[bool] = []

    class BarrierManager:
        def pause_binding(self, bid):
            return None

        def wait_binding_stopped(self, bid, timeout_seconds=5.0):
            delete_waiting.set()
            assert release_delete.wait(timeout=5.0)
            return True

        def resume_binding(self, bid, *, start=True):
            starts.append(start)

    monkeypatch.setattr(app.channels, "get_wecom_stream_manager", lambda: BarrierManager())
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: True)
    responses: dict[str, int] = {}

    def delete() -> None:
        response = _make_api_client(engine).delete(
            f"/api/enterprise/channels/{binding_id}",
            params={"tenant_id": "tenant_demo"},
            headers=_auth(users["owner"]),
        )
        responses["delete"] = response.status_code

    def update_credentials() -> None:
        response = _make_api_client(engine).post(
            f"/api/enterprise/channels/{binding_id}/wecom/credentials",
            json={
                "tenant_id": "tenant_demo",
                "bot_id": "aib_bot1",
                "secret": "new_secret",
                "corp_id": "corpA",
            },
            headers=_auth(users["owner"]),
        )
        responses["update"] = response.status_code

    delete_thread = threading.Thread(target=delete)
    update_thread = threading.Thread(target=update_credentials)
    delete_thread.start()
    assert delete_waiting.wait(timeout=5.0)
    update_thread.start()
    time.sleep(0.05)
    release_delete.set()
    delete_thread.join(timeout=10.0)
    update_thread.join(timeout=10.0)

    assert responses == {"delete": 204, "update": 404}
    assert starts == [False]
    with Session(engine) as db:
        assert db.get(ChannelBinding, binding_id) is None


# ---------- worker 退役收尾与群 chatid 降级 ----------


def test_worker_retirement_and_reconcile_rebuild(monkeypatch) -> None:
    engine = _test_engine()
    binding_id = _seed_wecom_binding(engine)
    started = threading.Event()
    release = threading.Event()
    processed: list[str] = []

    def slow_process_inbound(binding, inbound, *, db_engine=None):
        started.set()
        release.wait(timeout=5.0)
        processed.append(inbound.event_id)
        return True

    monkeypatch.setattr(intake_module, "process_inbound", slow_process_inbound)
    fake = FakeWSClient()
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: fake)
    manager.ensure_binding(binding_id)
    assert _wait_for(lambda: fake.connect_calls == 1)

    fake.emit_local("message", _text_frame(msgid="msg_slow"))
    assert started.wait(timeout=5.0)

    # 长任务中 stop:join 超时 → retired 置位,worker 完成当前条后自动退出清理
    manager.stop_binding(binding_id)
    state = manager._streams.get(binding_id)
    assert not manager.wait_binding_stopped(binding_id, timeout_seconds=0.2)
    assert state is not None and state.retired.is_set()

    release.set()
    assert _wait_for(lambda: not (state.worker and state.worker.is_alive()))
    assert processed == ["msg_slow"]
    # 退出路径已从注册表清除死 state,无残留
    assert manager._streams.get(binding_id) is None

    # 下轮 reconcile 正常重建且只有一份 state
    manager.reconcile_once()
    assert _wait_for(lambda: manager.get_stream(binding_id) is not None)
    assert len(list(manager._streams.values())) == 1
    manager.stop_binding(binding_id)


def test_group_without_chatid_degrades_to_p2p_with_warning(caplog) -> None:
    import logging

    frame = _text_frame(chattype="group", chatid="")
    with caplog.at_level(logging.WARNING, logger="app.channels.adapters.wecom"):
        inbound = normalize_wecom_frame(frame)
    assert inbound is not None
    # chattype=group 但缺 chatid:降级私聊,不退化为"每人一个群会话"
    assert inbound.is_group is False
    assert inbound.external_conv_id == "wecom_p2p_zhangsan"
    assert any("缺少 chatid" in record.message for record in caplog.records)

    # 有 chatid 的群形态不变
    group = normalize_wecom_frame(_text_frame(chatid="wr_1", chattype="group"))
    assert group is not None and group.is_group is True
    assert group.external_conv_id == "wecom_group_wr_1"


# ---------- 断开超时主动告警 ----------


def _seed_disconnect_alert_binding(engine, *, age_minutes: float) -> str:
    with Session(engine) as db:
        db.add(User(id="user_owner", tenant_id="tenant_demo", username="owner", password_hash="x"))
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="aib_bot1",
                external_user_id="owner_uid",
                staffdeck_user_id="user_owner",
                display_name="owner",
            )
        )
        db.add(
            ChatSession(
                id="s_owner",
                tenant_id="tenant_demo",
                user_id="user_owner",
                agent_id="agent_1",
                channel="wecom",
                external_conv_id="wecom_aib_bot1_p2p_owner_uid",
                channel_target_json={"to_user_id": "owner_uid", "context_token": "owner_uid"},
            )
        )
        binding_id = _seed_wecom_binding(engine)
        binding = db.get(ChannelBinding, binding_id)
        binding.connected = False
        binding.updated_at = utc_now() - timedelta(minutes=age_minutes)
        binding.last_connected_at = None
        db.add(binding)
        db.commit()
        return binding_id


def test_disconnect_timeout_alerts_once_and_clears_on_reconnect() -> None:
    from app.db.models import ChannelDelivery

    engine = _test_engine()
    binding_id = _seed_disconnect_alert_binding(engine, age_minutes=20)
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: FakeWSClient())

    # 阻止 reconcile 热拉起线程,专注告警判定
    manager.ensure_binding = lambda binding_id: None  # noqa: E731
    manager.reconcile_once()

    with Session(engine) as db:
        alerts = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "admin_alert")
        ).all()
        assert len(alerts) == 1
        assert "断开超过 15 分钟" in alerts[0].text
        assert alerts[0].target_json["to_user_id"] == "owner_uid"
        config = db.get(ChannelBinding, binding_id).config_json
        assert config["disconnect_alerted_at"]

        # 防重复:再次 reconcile 不再告警
        manager.reconcile_once()
        assert (
            db.exec(select(ChannelDelivery).where(ChannelDelivery.kind == "admin_alert")).all()
            == alerts
        )

        # 重连:清除告警标记并记录 last_connected_at,允许下次断连再告警
        manager._set_connected(binding_id, True)
        binding = db.get(ChannelBinding, binding_id)
        assert binding.connected is True
        assert binding.last_connected_at is not None
        assert "disconnect_alerted_at" not in (binding.config_json or {})


def test_disconnect_timeout_not_fired_before_threshold() -> None:
    from app.db.models import ChannelDelivery

    engine = _test_engine()
    binding_id = _seed_disconnect_alert_binding(engine, age_minutes=5)
    manager = WeComStreamManager(db_engine=engine, client_factory=lambda bot_id, secret: FakeWSClient())
    manager.ensure_binding = lambda binding_id: None  # noqa: E731
    manager.reconcile_once()
    with Session(engine) as db:
        assert db.exec(select(ChannelDelivery)).all() == []
        assert "disconnect_alerted_at" not in (db.get(ChannelBinding, binding_id).config_json or {})
