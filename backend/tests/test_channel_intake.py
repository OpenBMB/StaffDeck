import threading
import time
import os

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.channels.service_intake as intake_module
import app.core.agent_loop as agent_loop_module
from app.channels.service_identity import channel_username
from app.channels.service_intake import (
    _send_wechat_typing as _real_send_wechat_typing,
)
from app.channels.service_intake import (
    _session_lock,
    process_inbound,
)
from app.db.models import (
    ChannelBinding,
    ChannelDelivery,
    ChannelInboundEvent,
    ChatSession,
    Message,
    Tenant,
    User,
    new_id,
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


def _group_message(event_id: str = "evt_g1", text: str = "群里问一句") -> dict:
    msg = _p2p_message(event_id, text)
    msg["group_id"] = "room_123456"
    msg["session_id"] = "room_123456"
    return msg


def _seed_binding(engine, **overrides) -> str:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        values = {
            "tenant_id": "tenant_demo",
            "agent_id": "agent_1",
            "channel": "wechat",
            "status": "active",
            "config_json": {"ilink_bot_id": "bot_1@im.bot"},
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
    """替代真实 AgentLoop：记录请求并模拟用户/助手消息落库。"""

    calls: list = []
    error: Exception | None = None

    def __init__(self, db):
        self.db = db

    def handle_turn(self, request):
        type(self).calls.append(request)
        if type(self).error:
            raise type(self).error
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
                content="自动回复",
                metadata_json={},
            )
        )
        self.db.commit()


class TypingRecorder:
    """替代真实 typing 发送:记录 status 调用序列,隔离网络。"""

    calls: list[int] = []


def _record_typing(binding, ilink_user_id, context_token, status, **kwargs) -> None:
    TypingRecorder.calls.append(status)


@pytest.fixture(autouse=True)
def _fake_agent_loop(monkeypatch):
    RecordingAgentLoop.calls = []
    RecordingAgentLoop.error = None
    TypingRecorder.calls = []
    monkeypatch.setattr(agent_loop_module, "AgentLoop", RecordingAgentLoop)
    monkeypatch.setattr(intake_module, "_send_wechat_typing", _record_typing)
    yield


def test_inbound_runs_turn_and_marks_done() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_1"), db_engine=engine) is True
    assert len(RecordingAgentLoop.calls) == 1
    request = RecordingAgentLoop.calls[0]
    assert request.channel == "wechat"
    assert request.client_turn_id == "evt_1"
    assert request.agent_id == "agent_1"

    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "done"

        chat_session = db.get(ChatSession, request.session_id)
        assert chat_session.channel == "wechat"
        assert chat_session.external_conv_id == "wechat_p2p_user_ab12cd34@im.wechat"
        assert chat_session.channel_target_json == {
            "to_user_id": "user_ab12cd34@im.wechat",
            "context_token": "ctx_evt_1",
        }
        # 渠道用户已开通并映射
        user = db.get(User, chat_session.user_id)
        assert user.username.startswith("wechat_user_ab12cd34")


def test_event_id_replay_is_idempotent() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_dup"), db_engine=engine) is True
    # 重放同一 event:唯一冲突直接返回,不再跑对话
    assert process_inbound(binding, _p2p_message("evt_dup"), db_engine=engine) is False
    assert len(RecordingAgentLoop.calls) == 1

    with Session(engine) as db:
        events = db.exec(select(ChannelInboundEvent)).all()
        assert len(events) == 1
        assert events[0].status == "done"


def test_crash_recovery_dedup_marks_done_without_rerun() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)

    # 模拟崩溃现场:event 行丢失,但用户消息已带 client_turn_id 落库
    with Session(engine) as db:
        db.add(
            ChatSession(
                id="session_chan",
                tenant_id="tenant_demo",
                user_id="user_x",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_p2p_user_ab12cd34@im.wechat",
                channel_binding_id=binding_id,
            )
        )
        db.add(
            Message(
                id="msg_prev",
                tenant_id="tenant_demo",
                session_id="session_chan",
                role="user",
                content="你好",
                metadata_json={"client_turn_id": "evt_crash"},
            )
        )
        db.commit()

    binding = _load_binding(engine, binding_id)
    assert process_inbound(binding, _p2p_message("evt_crash"), db_engine=engine) is False
    assert RecordingAgentLoop.calls == []

    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "done"


def test_sweep_finds_turn_in_migration_isolated_session() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    original_conv = "wechat_p2p_user_ab12cd34@im.wechat"
    with Session(engine) as db:
        db.add(
            ChatSession(
                id="session_isolated",
                tenant_id="tenant_demo",
                user_id="old_shared_user",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id=(
                    f"legacy_ambiguous_identity:session_isolated:{original_conv}"
                ),
                channel_binding_id=binding_id,
            )
        )
        db.add(
            Message(
                id="msg_isolated_turn",
                tenant_id="tenant_demo",
                session_id="session_isolated",
                role="user",
                content="已落库",
                metadata_json={"client_turn_id": "evt_isolated"},
            )
        )
        db.commit()
    _seed_stale_event(
        engine,
        binding_id,
        "evt_isolated",
        status="processing",
        age_seconds=300,
        payload=_p2p_message("evt_isolated"),
        processor_run_id="old_process",
    )

    assert intake_module.sweep_stale_inbound_events(db_engine=engine) == 0
    assert RecordingAgentLoop.calls == []
    with Session(engine) as db:
        event = db.exec(
            select(ChannelInboundEvent).where(
                ChannelInboundEvent.event_id == "evt_isolated"
            )
        ).one()
        assert event.status == "failed"
        assert event.error == "process_exit_incomplete_turn"


def test_group_message_uses_group_account_and_sender_prefix() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _group_message("evt_g1"), db_engine=engine) is True
    assert len(RecordingAgentLoop.calls) == 1
    request = RecordingAgentLoop.calls[0]
    assert request.message.startswith("[发送者: 微信用户 ")
    assert "群里问一句" in request.message

    with Session(engine) as db:
        chat_session = db.get(ChatSession, request.session_id)
        assert chat_session.external_conv_id == "wechat_group_room_123456"
        # 群消息回复投递到群会话而不是发言人
        assert chat_session.channel_target_json["to_user_id"] == "room_123456"
        group_user = db.get(User, chat_session.user_id)
        assert group_user.username == channel_username(
            "tenant_demo", "wechat", "group:room_123456", ""
        )


def test_failure_marks_event_failed_and_stages_error_notice() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)
    RecordingAgentLoop.error = RuntimeError("模型配置缺失")

    assert process_inbound(binding, _p2p_message("evt_err"), db_engine=engine) is False

    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "failed"
        assert "模型配置缺失" in (event.error or "")

        notices = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "error_notice")
        ).all()
        assert len(notices) == 1
        assert notices[0].status == "pending"
        assert notices[0].text == "处理出错，请稍后再试。"
        assert notices[0].target_json["to_user_id"] == "user_ab12cd34@im.wechat"


def test_non_text_message_is_dropped_silently() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)

    image_msg = _p2p_message("evt_img")
    image_msg["item_list"] = [{"type": 2, "image_item": {"url": "https://x"}}]
    assert process_inbound(binding, image_msg, db_engine=engine) is False
    assert RecordingAgentLoop.calls == []
    with Session(engine) as db:
        assert db.exec(select(ChannelInboundEvent)).all() == []


def test_session_serial_lock_shared_per_session() -> None:
    assert _session_lock("s1") is _session_lock("s1")
    assert _session_lock("s1") is not _session_lock("s2")


def test_concurrent_inbound_same_conversation_is_serialized(tmp_path) -> None:
    # 文件库 + busy timeout,避免内存库单连接下的并发事务冲突
    engine = create_engine(
        f"sqlite:///{tmp_path / 'intake.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)

    # 先串行跑一条,确保会话已锚定(并发锚定由唯一索引兜底,测试库无索引)
    assert process_inbound(binding, _p2p_message("evt_warm"), db_engine=engine) is True

    active = {"count": 0, "max": 0}
    guard = threading.Lock()

    class SlowLoop(RecordingAgentLoop):
        def handle_turn(self, request):
            with guard:
                active["count"] += 1
                active["max"] = max(active["max"], active["count"])
            time.sleep(0.05)
            with guard:
                active["count"] -= 1
            super().handle_turn(request)

    RecordingAgentLoop.calls = []
    original = agent_loop_module.AgentLoop
    agent_loop_module.AgentLoop = SlowLoop
    threads = [
        threading.Thread(
            target=process_inbound,
            args=(binding, _p2p_message(f"evt_c{index}")),
            kwargs={"db_engine": engine},
        )
        for index in range(4)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        agent_loop_module.AgentLoop = original

    assert len(RecordingAgentLoop.calls) == 4
    assert active["max"] == 1


def test_typing_wraps_handle_turn_success() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_t1"), db_engine=engine) is True
    assert TypingRecorder.calls == [1, 2]


def test_typing_cancelled_on_turn_failure() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)
    RecordingAgentLoop.error = RuntimeError("模型配置缺失")

    assert process_inbound(binding, _p2p_message("evt_t2"), db_engine=engine) is False
    assert TypingRecorder.calls == [1, 2]


class FakeTypingClient:
    def __init__(
        self,
        *,
        ticket: str = "ticket_1",
        get_config_error: Exception | None = None,
        send_error: Exception | None = None,
    ):
        self.ticket = ticket
        self.get_config_error = get_config_error
        self.send_error = send_error
        self.get_config_calls: list[tuple[str, str]] = []
        self.send_calls: list[tuple[str, str, int]] = []

    def get_config(self, ilink_user_id: str, context_token: str = "") -> dict:
        self.get_config_calls.append((ilink_user_id, context_token))
        if self.get_config_error:
            raise self.get_config_error
        return {"typing_ticket": self.ticket}

    def send_typing(self, ilink_user_id: str, typing_ticket: str, status: int = 1) -> None:
        self.send_calls.append((ilink_user_id, typing_ticket, status))
        if self.send_error:
            raise self.send_error


def test_typing_fetches_and_caches_ticket() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)
    client = FakeTypingClient()

    _real_send_wechat_typing(
        binding, "user_ab12cd34@im.wechat", "ctx_1", 1,
        db_engine=engine, client_factory=lambda row: client,
    )

    assert client.get_config_calls == [("user_ab12cd34@im.wechat", "ctx_1")]
    assert client.send_calls == [("user_ab12cd34@im.wechat", "ticket_1", 1)]
    with Session(engine) as db:
        row = db.get(ChannelBinding, binding_id)
        assert row.config_json["typing_ticket"] == "ticket_1"


def test_typing_reuses_cached_ticket_without_get_config() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, config_json={"typing_ticket": "cached_ticket"})
    binding = _load_binding(engine, binding_id)
    client = FakeTypingClient()

    _real_send_wechat_typing(
        binding, "user_1", "ctx_1", 1,
        db_engine=engine, client_factory=lambda row: client,
    )

    assert client.get_config_calls == []
    assert client.send_calls == [("user_1", "cached_ticket", 1)]


def test_typing_skips_silently_when_get_config_fails() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)
    client = FakeTypingClient(get_config_error=RuntimeError("网络不通"))

    # 不抛异常、不发送、不写缓存
    _real_send_wechat_typing(
        binding, "user_1", "ctx_1", 1,
        db_engine=engine, client_factory=lambda row: client,
    )

    assert client.send_calls == []
    with Session(engine) as db:
        row = db.get(ChannelBinding, binding_id)
        assert "typing_ticket" not in (row.config_json or {})


def test_typing_send_failure_clears_cached_ticket() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, config_json={"typing_ticket": "stale_ticket"})
    binding = _load_binding(engine, binding_id)
    client = FakeTypingClient(send_error=RuntimeError("ticket 失效"))

    _real_send_wechat_typing(
        binding, "user_1", "ctx_1", 1,
        db_engine=engine, client_factory=lambda row: client,
    )

    assert client.send_calls == [("user_1", "stale_ticket", 1)]
    with Session(engine) as db:
        row = db.get(ChannelBinding, binding_id)
        assert "typing_ticket" not in (row.config_json or {})


def test_typing_cancel_without_ticket_is_noop() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)
    client = FakeTypingClient()

    _real_send_wechat_typing(
        binding, "user_1", "ctx_1", 2,
        db_engine=engine, client_factory=lambda row: client,
    )

    # cancel 不触发 get_config,无 ticket 直接跳过
    assert client.get_config_calls == []
    assert client.send_calls == []


def test_typing_noop_for_inactive_binding() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, status="expired")
    binding = _load_binding(engine, binding_id)
    client = FakeTypingClient()

    _real_send_wechat_typing(
        binding, "user_1", "ctx_1", 1,
        db_engine=engine, client_factory=lambda row: client,
    )

    assert client.get_config_calls == []
    assert client.send_calls == []


# ---------- 陈旧 processing 事件接管与启动 sweep ----------


def _seed_stale_event(
    engine,
    binding_id: str,
    event_id: str,
    *,
    status: str,
    age_seconds: float,
    payload: dict | None = None,
    processor_run_id: str | None = None,
) -> None:
    from datetime import timedelta

    with Session(engine) as db:
        db.add(
            ChannelInboundEvent(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                channel="wechat",
                event_id=event_id,
                payload_json=payload or {},
                status=status,
                processor_run_id=processor_run_id,
                updated_at=utc_now() - timedelta(seconds=age_seconds),
            )
        )
        db.commit()


def test_stale_processing_event_is_taken_over() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_stale_event(engine, binding_id, "evt_stale", status="processing", age_seconds=300)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_stale"), db_engine=engine) is True
    assert len(RecordingAgentLoop.calls) == 1
    with Session(engine) as db:
        events = db.exec(select(ChannelInboundEvent)).all()
        assert len(events) == 1
        assert events[0].status == "done"


def test_fresh_processing_event_is_not_killed() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_stale_event(
        engine,
        binding_id,
        "evt_fresh",
        status="processing",
        age_seconds=5,
        processor_run_id=intake_module.current_processor_run_id(),
    )
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_fresh"), db_engine=engine) is False
    assert RecordingAgentLoop.calls == []
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "processing"


def test_current_run_processing_event_is_never_taken_over_by_age() -> None:
    from app.channels.service_intake import sweep_stale_inbound_events

    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_stale_event(
        engine,
        binding_id,
        "evt_long_turn",
        status="processing",
        age_seconds=900,
        payload=_p2p_message("evt_long_turn"),
        processor_run_id=intake_module.current_processor_run_id(),
    )
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_long_turn"), db_engine=engine) is False
    assert sweep_stale_inbound_events(db_engine=engine) == 0
    assert RecordingAgentLoop.calls == []
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "processing"
        assert event.processor_run_id == intake_module.current_processor_run_id()


def test_stale_claim_is_released_when_recovery_logic_raises(monkeypatch) -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_stale_event(
        engine,
        binding_id,
        "evt_recovery_error",
        status="processing",
        age_seconds=300,
        payload=_p2p_message("evt_recovery_error"),
        processor_run_id="old_process",
    )
    binding = _load_binding(engine, binding_id)

    monkeypatch.setattr(
        intake_module,
        "_find_turn_user_message_in_conv",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        intake_module,
        "resolve_or_provision_user",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("recovery db failure")
        ),
    )
    with pytest.raises(RuntimeError, match="recovery db failure"):
        process_inbound(binding, _p2p_message("evt_recovery_error"), db_engine=engine)

    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "processing"
        assert event.processor_run_id is None


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_processor_run_id_changes_after_prefork() -> None:
    parent_run_id = intake_module.current_processor_run_id()
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.close(read_fd)
            child_run_id = intake_module.current_processor_run_id().encode()
            os.write(write_fd, child_run_id)
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    child_run_id = os.read(read_fd, 128).decode()
    os.close(read_fd)
    _, status = os.waitpid(child_pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert child_run_id
    assert child_run_id != parent_run_id


def test_concurrent_sweeps_claim_old_event_once(tmp_path) -> None:
    from app.channels.service_intake import sweep_stale_inbound_events

    db_path = tmp_path / "sweep.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    binding_id = _seed_binding(engine)
    _seed_stale_event(
        engine,
        binding_id,
        "evt_sweep_race",
        status="processing",
        age_seconds=300,
        payload=_p2p_message("evt_sweep_race"),
        processor_run_id="old_process",
    )
    gate = threading.Barrier(2)
    results: list[int] = []
    errors: list[Exception] = []

    def sweep() -> None:
        try:
            gate.wait(timeout=5.0)
            results.append(sweep_stale_inbound_events(db_engine=engine))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=sweep) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert sum(results) == 1
    assert len(RecordingAgentLoop.calls) == 1
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "done"
        assert event.processor_run_id == intake_module.current_processor_run_id()


def test_done_event_is_never_taken_over() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_stale_event(engine, binding_id, "evt_done", status="done", age_seconds=300)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_done"), db_engine=engine) is False
    assert RecordingAgentLoop.calls == []
    with Session(engine) as db:
        assert db.exec(select(ChannelInboundEvent)).one().status == "done"


def test_startup_sweep_takes_over_stale_events() -> None:
    from app.channels.service_intake import sweep_stale_inbound_events

    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_stale_event(
        engine,
        binding_id,
        "evt_sweep",
        status="processing",
        age_seconds=300,
        payload=_p2p_message("evt_sweep"),
    )
    # 新鲜的与 done 的不应被接管
    _seed_stale_event(
        engine,
        binding_id,
        "evt_fresh",
        status="processing",
        age_seconds=5,
        processor_run_id=intake_module.current_processor_run_id(),
    )
    _seed_stale_event(engine, binding_id, "evt_done", status="done", age_seconds=300)

    taken = sweep_stale_inbound_events(db_engine=engine)
    assert taken == 1
    assert len(RecordingAgentLoop.calls) == 1
    with Session(engine) as db:
        by_event = {row.event_id: row.status for row in db.exec(select(ChannelInboundEvent)).all()}
        assert by_event == {"evt_sweep": "done", "evt_fresh": "processing", "evt_done": "done"}


# ---------- 崩溃恢复:turn 未完成窗口 ----------


def _seed_incomplete_turn(engine, binding_id: str, event_id: str, *, with_reply: bool) -> None:
    from datetime import timedelta

    with Session(engine) as db:
        db.add(
            ChatSession(
                id="session_incomplete",
                tenant_id="tenant_demo",
                user_id="user_x",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_p2p_user_ab12cd34@im.wechat",
                channel_binding_id=binding_id,
            )
        )
        db.add(
            Message(
                id="msg_turn_user",
                tenant_id="tenant_demo",
                session_id="session_incomplete",
                role="user",
                content="你好",
                metadata_json={"client_turn_id": event_id},
            )
        )
        if with_reply:
            db.add(
                Message(
                    id="msg_turn_reply",
                    tenant_id="tenant_demo",
                    session_id="session_incomplete",
                    role="assistant",
                    content="回复",
                    metadata_json={"turn_id": "msg_turn_user", "user_message_id": "msg_turn_user"},
                )
            )
        db.add(
            ChannelInboundEvent(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                channel="wechat",
                event_id=event_id,
                payload_json={},
                status="processing",
                updated_at=utc_now() - timedelta(seconds=300),
            )
        )
        db.commit()


def test_incomplete_turn_marks_failed_and_notices_without_rerun() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_incomplete_turn(engine, binding_id, "evt_gap", with_reply=False)
    binding = _load_binding(engine, binding_id)

    # 不重跑:标记 failed + 中断通知投递
    assert process_inbound(binding, _p2p_message("evt_gap"), db_engine=engine) is False
    assert RecordingAgentLoop.calls == []
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "failed"
        assert event.error == "process_exit_incomplete_turn"
        notices = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "error_notice")
        ).all()
        assert len(notices) == 1
        assert notices[0].text == "上一条消息处理中断，请重新发送。"
        assert notices[0].session_id == "session_incomplete"


def test_repeated_incomplete_turn_recovery_stages_one_notice() -> None:
    from datetime import timedelta

    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_incomplete_turn(engine, binding_id, "evt_gap_repeat", with_reply=False)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_gap_repeat"), db_engine=engine) is False
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        event.status = "processing"
        event.updated_at = utc_now() - timedelta(seconds=300)
        db.add(event)
        db.commit()

    assert process_inbound(binding, _p2p_message("evt_gap_repeat"), db_engine=engine) is False
    with Session(engine) as db:
        notices = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "error_notice")
        ).all()
        assert len(notices) == 1
        assert notices[0].idempotency_key == f"channel-interrupted:{binding_id}:evt_gap_repeat"


def test_completed_turn_is_not_misflagged() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_incomplete_turn(engine, binding_id, "evt_done_turn", with_reply=True)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_done_turn"), db_engine=engine) is False
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        # 已有完成回复:收敛为 done,不标 failed、不发通知
        assert event.status == "done"
        assert event.processed_at is not None
        assert db.exec(select(ChannelDelivery)).all() == []


def test_sweep_marks_incomplete_turn_failed_consistently() -> None:
    from app.channels.service_intake import sweep_stale_inbound_events

    engine = _test_engine()
    binding_id = _seed_binding(engine)
    _seed_incomplete_turn(engine, binding_id, "evt_gap_sweep", with_reply=False)
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        event.payload_json = _p2p_message("evt_gap_sweep")
        db.add(event)
        db.commit()

    taken = sweep_stale_inbound_events(db_engine=engine)
    # sweep 与运行时接管一致:不重跑,标 failed + 通知
    assert taken == 0
    assert RecordingAgentLoop.calls == []
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "failed"
        assert event.error == "process_exit_incomplete_turn"
        assert db.exec(select(ChannelDelivery).where(ChannelDelivery.kind == "error_notice")).all()


def test_concurrent_sweeps_stage_one_incomplete_notice(tmp_path) -> None:
    from app.channels.service_intake import sweep_stale_inbound_events

    db_path = tmp_path / "incomplete-sweep.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    binding_id = _seed_binding(engine)
    _seed_incomplete_turn(engine, binding_id, "evt_gap_race", with_reply=False)
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        event.payload_json = _p2p_message("evt_gap_race")
        event.processor_run_id = "old_process"
        db.add(event)
        db.commit()
    gate = threading.Barrier(2)
    errors: list[Exception] = []

    def sweep() -> None:
        try:
            gate.wait(timeout=5.0)
            sweep_stale_inbound_events(db_engine=engine)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=sweep) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "failed"
        notices = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "error_notice")
        ).all()
        assert len(notices) == 1
