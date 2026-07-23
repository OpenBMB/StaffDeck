from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.adapters.base import register_channel_adapter
from app.channels.service_outbox import run_delivery_daemon, stage_channel_delivery
from app.config import get_settings
from app.db.models import ChannelBinding, ChannelDelivery, ChatSession, Message, Tenant, utc_now


class FakeAdapter:
    def __init__(self, *, fail_times: int = 0):
        self.fail_times = fail_times
        self.sent: list[tuple[str, dict, str]] = []

    def send(self, binding: ChannelBinding, target: dict, text: str) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟发送失败")
        self.sent.append((binding.id, target, text))


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_binding(db: Session, *, channel: str = "fake", status: str = "active") -> ChannelBinding:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    binding = ChannelBinding(
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel=channel,
        status=status,
        external_account_key=f"{channel}:account",
    )
    db.add(binding)
    db.commit()
    return binding


def _channel_session(binding: ChannelBinding) -> ChatSession:
    return ChatSession(
        id="session_chan",
        tenant_id=binding.tenant_id,
        user_id="user_1",
        agent_id=binding.agent_id,
        channel=binding.channel,
        external_conv_id="fake_p2p_u1",
        channel_target_json={"to_user_id": "u1", "context_token": "ctx"},
        channel_binding_id=binding.id,
        channel_account_key=binding.external_account_key,
    )


def _assistant_message(session_id: str, message_id: str, content: str = "回复内容") -> Message:
    return Message(
        id=message_id,
        tenant_id="tenant_demo",
        session_id=session_id,
        role="assistant",
        content=content,
    )


def test_web_session_is_not_staged() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        web_session = ChatSession(id="session_web", tenant_id="tenant_demo", agent_id="agent_1")
        message = _assistant_message("session_web", "msg_web")
        db.add(web_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, web_session, message)
        db.commit()
        assert db.exec(select(ChannelDelivery)).all() == []


def test_channel_session_stages_delivery_in_same_transaction() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        binding = _seed_binding(db)
        chat_session = _channel_session(binding)
        message = _assistant_message(chat_session.id, "msg_chan")
        db.add(chat_session)
        db.add(message)
        db.commit()

        # staging 不 commit,随主事务一起落库
        stage_channel_delivery(db, chat_session, message)
        db.commit()

        deliveries = db.exec(select(ChannelDelivery)).all()
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert delivery.binding_id == binding.id
        assert delivery.session_id == chat_session.id
        assert delivery.message_id == "msg_chan"
        assert delivery.idempotency_key == "msg_chan"
        assert delivery.kind == "reply"
        assert delivery.status == "pending"
        assert delivery.target_json == {"to_user_id": "u1", "context_token": "ctx"}


def test_staging_is_idempotent_per_message() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        binding = _seed_binding(db)
        chat_session = _channel_session(binding)
        message = _assistant_message(chat_session.id, "msg_chan")
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        stage_channel_delivery(db, chat_session, message)
        db.commit()
        assert len(db.exec(select(ChannelDelivery)).all()) == 1


def test_staging_never_breaks_main_flow() -> None:
    class BrokenDb:
        def exec(self, _statement):
            raise RuntimeError("db 炸了")

    chat_session = ChatSession(
        id="session_chan",
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel="fake",
        channel_target_json={"to_user_id": "u1", "context_token": "ctx"},
    )
    message = _assistant_message("session_chan", "msg_x")
    # 不抛异常,只记日志
    stage_channel_delivery(BrokenDb(), chat_session, message)


def test_legacy_session_claim_conflict_does_not_poison_main_transaction() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        binding = _seed_binding(db)
        existing = _channel_session(binding)
        existing.id = "session_existing"
        legacy = _channel_session(binding)
        legacy.id = "session_legacy"
        legacy.channel_binding_id = None
        message = _assistant_message(legacy.id, "msg_legacy")
        db.add(existing)
        db.add(legacy)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, legacy, message)
        message.content = "主事务仍可提交"
        db.add(message)
        db.commit()

        db.refresh(legacy)
        assert legacy.channel_binding_id is None
        assert db.exec(select(ChannelDelivery)).all() == []
        assert db.get(Message, message.id).content == "主事务仍可提交"


def test_missing_target_skips_staging() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        binding = _seed_binding(db)
        chat_session = _channel_session(binding)
        chat_session.channel_target_json = None
        message = _assistant_message(chat_session.id, "msg_chan")
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()
        assert db.exec(select(ChannelDelivery)).all() == []


def _make_delivery(db: Session, binding: ChannelBinding, **overrides) -> ChannelDelivery:
    values = {
        "tenant_id": binding.tenant_id,
        "binding_id": binding.id,
        "session_id": "session_chan",
        "message_id": "msg_chan",
        "target_json": {"to_user_id": "u1", "context_token": "ctx"},
        "kind": "reply",
        "text": "回复内容",
        "status": "pending",
        "next_attempt_at": utc_now(),
        "idempotency_key": "msg_chan",
    }
    values.update(overrides)
    session_id = values["session_id"]
    if not db.get(ChatSession, session_id):
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=binding.tenant_id,
                agent_id=binding.agent_id,
                channel=binding.channel,
                channel_binding_id=binding.id,
                channel_account_key=binding.external_account_key,
            )
        )
    delivery = ChannelDelivery(**values)
    db.add(delivery)
    db.commit()
    return delivery


def test_daemon_delivers_pending() -> None:
    engine = _test_engine()
    adapter = FakeAdapter()
    register_channel_adapter("fake", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db)
        binding_id = binding.id
        delivery = _make_delivery(db, binding)
        delivery_id = delivery.id

    run_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "delivered"
        assert delivery.delivered_at is not None
        assert delivery.attempts == 1
    assert adapter.sent == [(binding_id, {"to_user_id": "u1", "context_token": "ctx"}, "回复内容")]


def test_daemon_rejects_reply_when_session_account_does_not_match_binding() -> None:
    engine = _test_engine()
    adapter = FakeAdapter()
    register_channel_adapter("fake", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db)
        chat_session = _channel_session(binding)
        chat_session.channel_account_key = "fake:other-account"
        db.add(chat_session)
        db.commit()
        delivery = _make_delivery(db, binding)
        delivery_id = delivery.id

    run_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "failed"
        assert delivery.last_error == "渠道会话与绑定账号不一致"
    assert adapter.sent == []


def test_daemon_retries_with_backoff_then_fails(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeAdapter(fail_times=10)
    register_channel_adapter("fake", adapter)
    settings = get_settings().model_copy(update={"channel_delivery_max_attempts": 2})
    monkeypatch.setattr("app.channels.service_outbox.get_settings", lambda: settings)

    with Session(engine) as db:
        binding = _seed_binding(db)
        delivery = _make_delivery(db, binding)
        delivery_id = delivery.id

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "pending"
        assert delivery.attempts == 1
        assert delivery.last_error == "模拟发送失败"
        assert delivery.next_attempt_at > utc_now()
        backoff = (delivery.next_attempt_at - utc_now()).total_seconds()
        assert 0 < backoff <= 4

        # 到期后重试,达到最大次数置 failed
        delivery.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.add(delivery)
        db.commit()

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "failed"
        assert delivery.attempts == 2
        assert delivery.next_attempt_at is None


def test_daemon_recovers_then_delivers() -> None:
    engine = _test_engine()
    adapter = FakeAdapter(fail_times=1)
    register_channel_adapter("fake", adapter)

    with Session(engine) as db:
        binding = _seed_binding(db)
        delivery = _make_delivery(db, binding)
        delivery_id = delivery.id

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "pending"
        delivery.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.add(delivery)
        db.commit()

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "delivered"
        assert delivery.attempts == 2


def test_daemon_resets_stuck_sending() -> None:
    engine = _test_engine()
    adapter = FakeAdapter()
    register_channel_adapter("fake", adapter)

    with Session(engine) as db:
        binding = _seed_binding(db)
        delivery = _make_delivery(db, binding, status="sending", attempts=3)
        delivery_id = delivery.id

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "delivered"
        assert delivery.attempts == 4


def test_daemon_fails_delivery_for_inactive_binding() -> None:
    engine = _test_engine()
    adapter = FakeAdapter()
    register_channel_adapter("fake", adapter)

    with Session(engine) as db:
        binding = _seed_binding(db, status="disabled")
        delivery = _make_delivery(db, binding)
        delivery_id = delivery.id

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "failed"
        assert "停用" in (delivery.last_error or "")
    assert adapter.sent == []


def test_daemon_skips_future_deliveries() -> None:
    engine = _test_engine()
    adapter = FakeAdapter()
    register_channel_adapter("fake", adapter)

    with Session(engine) as db:
        binding = _seed_binding(db)
        delivery = _make_delivery(db, binding, next_attempt_at=utc_now() + timedelta(hours=1))
        delivery_id = delivery.id

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "pending"
        assert delivery.attempts == 0


def test_unregistered_channel_marks_failed_eventually(monkeypatch) -> None:
    engine = _test_engine()
    settings = get_settings().model_copy(update={"channel_delivery_max_attempts": 1})
    monkeypatch.setattr("app.channels.service_outbox.get_settings", lambda: settings)

    with Session(engine) as db:
        binding = _seed_binding(db, channel="unknown_channel")
        delivery = _make_delivery(db, binding)
        delivery_id = delivery.id

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "failed"
        assert "未注册" in (delivery.last_error or "")


@pytest.fixture(autouse=True)
def _clean_adapter_registry():
    yield
    from app.channels.adapters.base import _adapters

    _adapters.pop("fake", None)
    _adapters.pop("unknown_channel", None)
