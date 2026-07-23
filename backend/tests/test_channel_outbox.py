from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.adapters.base import register_channel_adapter
from app.channels.service_outbox import run_delivery_daemon, stage_channel_delivery
from app.config import get_settings
from app.channels.crypto import encrypt_channel_secret
from app.db.models import (
    ChannelBinding,
    ChannelDelivery,
    ChannelIdentity,
    ChatSession,
    Message,
    Tenant,
    User,
    utc_now,
)


class FakeAdapter:
    def __init__(self, *, fail_times: int = 0):
        self.fail_times = fail_times
        self.sent: list[tuple[str, dict, str]] = []
        self.dedupe_keys: list[str | None] = []

    def send(self, binding: ChannelBinding, target: dict, text: str, *, dedupe_key: str | None = None) -> None:
        self.dedupe_keys.append(dedupe_key)
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


# ---------- 原子 claim 与确定性幂等 ----------


def test_concurrent_daemons_claim_disjoint_deliveries(tmp_path) -> None:
    import threading

    from app.channels.service_outbox import _claim_due_deliveries

    engine = create_engine(
        f"sqlite:///{tmp_path / 'outbox_claim.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        binding = _seed_binding(db)
        all_ids = set()
        for index in range(30):
            delivery = _make_delivery(db, binding, message_id=f"msg_{index}", idempotency_key=f"msg_{index}")
            all_ids.add(delivery.id)

    claimed: list[set] = []
    barrier = threading.Barrier(3)

    def claim() -> None:
        barrier.wait()
        with Session(engine) as db:
            claimed.append({row.id for row in _claim_due_deliveries(db, limit=20)})

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=30)

    assert len(claimed) == 2
    # 两个并发守护拿到互不重叠的行集,且合起来覆盖全部到期投递
    assert claimed[0].isdisjoint(claimed[1])
    assert claimed[0] | claimed[1] == all_ids


def test_delivery_retries_pass_same_dedupe_key() -> None:
    engine = _test_engine()
    adapter = FakeAdapter(fail_times=1)
    register_channel_adapter("fake", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db)
        delivery = _make_delivery(db, binding)
        delivery_id = delivery.id
        idem = delivery.idempotency_key

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "pending"
        delivery.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.add(delivery)
        db.commit()

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        assert db.get(ChannelDelivery, delivery_id).status == "delivered"
    # 同一投递的每次重试都把 idempotency_key 作为 dedupe_key 传给适配器
    assert adapter.dedupe_keys == [idem, idem]


def test_claim_orders_by_next_attempt_at() -> None:
    engine = _test_engine()
    adapter = FakeAdapter()
    register_channel_adapter("fake", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db)
        _make_delivery(
            db,
            binding,
            message_id="msg_late",
            idempotency_key="msg_late",
            next_attempt_at=utc_now() + timedelta(hours=1),
        )
        early = _make_delivery(
            db,
            binding,
            message_id="msg_early",
            idempotency_key="msg_early",
            next_attempt_at=utc_now() - timedelta(seconds=10),
        )
        early_id = early.id

    run_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        assert db.get(ChannelDelivery, early_id).status == "delivered"
        # 未到期的不被 claim
        late = db.exec(select(ChannelDelivery).where(ChannelDelivery.idempotency_key == "msg_late")).one()
        assert late.status == "pending"
        assert late.sending_since is None


# ---------- 渠道异常主动告警 ----------


def _seed_alertable_wechat_binding(engine, *, with_identity: bool, with_session: bool) -> str:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(User(id="user_web", tenant_id="tenant_demo", username="zhangsan", password_hash="x"))
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wechat",
            status="active",
            connected=True,
            credentials_enc=encrypt_channel_secret("tok"),
            config_json={"baseurl": "https://ilinkai.weixin.qq.com", "ilink_bot_id": "bot@im.bot"},
            created_by_user_id="user_web",
        )
        db.add(binding)
        db.flush()
        if with_identity:
            db.add(
                ChannelIdentity(
                    tenant_id="tenant_demo",
                    channel="wechat",
                    external_account_scope="",
                    external_user_id="wxid_creator",
                    staffdeck_user_id="user_web",
                    display_name="张三",
                )
            )
        if with_session:
            db.add(
                ChatSession(
                    id="s_creator",
                    tenant_id="tenant_demo",
                    user_id="user_web",
                    agent_id="agent_1",
                    channel="wechat",
                    external_conv_id="wechat_p2p_wxid_creator",
                    channel_target_json={"to_user_id": "wxid_creator", "context_token": "ctx_1"},
                    channel_binding_id=binding.id,
                )
            )
        db.commit()
        return binding.id


def test_wechat_expired_alerts_creator_via_admin_alert() -> None:
    from app.channels.adapters.wechat import WeChatPollManager

    engine = _test_engine()
    binding_id = _seed_alertable_wechat_binding(engine, with_identity=True, with_session=True)
    manager = WeChatPollManager(db_engine=engine)
    manager._mark_session_expired(binding_id)

    with Session(engine) as db:
        alerts = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "admin_alert")
        ).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert "微信渠道 token 已失效" in alert.text
        assert alert.binding_id == binding_id
        # 目标取创建者最近私聊会话的 channel_target_json
        assert alert.target_json == {"to_user_id": "wxid_creator", "context_token": "ctx_1"}
        assert alert.session_id == "s_creator"
        assert db.get(ChannelBinding, binding_id).status == "expired"


def test_notify_skips_when_creator_has_no_identity() -> None:
    from app.channels.adapters.wechat import WeChatPollManager

    engine = _test_engine()
    binding_id = _seed_alertable_wechat_binding(engine, with_identity=False, with_session=False)
    manager = WeChatPollManager(db_engine=engine)
    # 无身份:跳过仅记日志,不影响主流程(过期标记照常落)
    manager._mark_session_expired(binding_id)
    with Session(engine) as db:
        assert db.exec(select(ChannelDelivery)).all() == []
        assert db.get(ChannelBinding, binding_id).status == "expired"


def test_notify_uses_identity_basics_without_session() -> None:
    from app.channels.adapters.wechat import WeChatPollManager

    engine = _test_engine()
    binding_id = _seed_alertable_wechat_binding(engine, with_identity=True, with_session=False)
    manager = WeChatPollManager(db_engine=engine)
    manager._mark_session_expired(binding_id)
    with Session(engine) as db:
        alerts = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "admin_alert")
        ).all()
        assert len(alerts) == 1
        # 无会话:按身份基本信息构造 to_user_id
        assert alerts[0].target_json["to_user_id"] == "wxid_creator"
        assert alerts[0].session_id.startswith("alert:")
