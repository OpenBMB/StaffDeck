import threading
from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.adapters import base as adapter_registry
from app.channels.adapters.base import register_channel_adapter
from app.channels.service_durable_inbox import reaction_target
from app.channels.service_outbox import (
    cleanup_channel_reactions_before_binding_delete,
    run_delivery_daemon,
    run_reaction_delivery_daemon,
    stage_channel_delivery,
)
from app.config import get_settings
from app.channels.crypto import encrypt_channel_secret
from app.db.models import (
    ChannelBinding,
    ChannelDelivery,
    ChannelIdentity,
    ChannelInboundEvent,
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

    def send(
        self,
        binding: ChannelBinding,
        target: dict,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        self.dedupe_keys.append(idempotency_key)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟发送失败")
        self.sent.append((binding.id, target, text))


class FakeFeishuAdapter(FakeAdapter):
    reaction_token = "Get"
    reaction_attach_idempotent = False

    def __init__(
        self,
        *,
        fail_reaction_add: bool = False,
        recovered_reaction_id: str | None = None,
    ):
        super().__init__()
        self.fail_reaction_add = fail_reaction_add
        self.recovered_reaction_id = recovered_reaction_id
        self.calls: list[tuple[str, str, str]] = []
        self.reaction_targets: list[dict] = []

    def add_reaction(
        self,
        binding: ChannelBinding,
        target: dict,
        emoji_type: str,
    ) -> str:
        self.reaction_targets.append(dict(target))
        self.calls.append(("add", str(target.get("message_id") or ""), emoji_type))
        if self.fail_reaction_add:
            raise RuntimeError("reaction unavailable")
        return "reaction_123"

    def find_own_reaction(
        self,
        binding: ChannelBinding,
        target: dict,
        emoji_type: str,
    ) -> str | None:
        self.reaction_targets.append(dict(target))
        self.calls.append(("find", str(target.get("message_id") or ""), emoji_type))
        return self.recovered_reaction_id

    def remove_reaction(
        self,
        binding: ChannelBinding,
        target: dict,
        reaction_id: str,
    ) -> None:
        self.reaction_targets.append(dict(target))
        self.calls.append(("remove", str(target.get("message_id") or ""), reaction_id))

    def send(
        self,
        binding: ChannelBinding,
        target: dict,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        self.calls.append(("send", str(target.get("message_id") or ""), text))
        super().send(binding, target, text, idempotency_key=idempotency_key)


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


def test_public_api_session_returns_through_api_without_channel_delivery() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        api_session = ChatSession(
            id="session_public_api",
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="public_api",
        )
        message = _assistant_message("session_public_api", "msg_public_api")
        db.add(api_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, api_session, message)
        db.commit()

        assert db.exec(select(ChannelDelivery)).all() == []


def test_skill_test_session_returns_through_debug_stream_without_channel_delivery() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        skill_test_session = ChatSession(
            id="session_skill_test",
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="skill_test",
        )
        message = _assistant_message("session_skill_test", "msg_skill_test")
        db.add(skill_test_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, skill_test_session, message)
        db.commit()

        assert db.exec(select(ChannelDelivery)).all() == []


def test_pilotdeck_legacy_session_returns_through_api_without_channel_delivery() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        api_session = ChatSession(
            id="session_pilotdeck",
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="pilotdeck_group_chat",
        )
        message = _assistant_message("session_pilotdeck", "msg_pilotdeck")
        db.add(api_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, api_session, message)
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


def test_staging_is_idempotent_per_inbound_turn() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        binding = _seed_binding(db)
        chat_session = _channel_session(binding)
        user_message = Message(
            id="msg_user_turn",
            tenant_id=binding.tenant_id,
            session_id=chat_session.id,
            role="user",
            content="hello",
            metadata_json={"client_turn_id": "event_turn_1"},
        )
        first = _assistant_message(chat_session.id, "msg_assistant_first")
        first.metadata_json = {"user_message_id": user_message.id}
        duplicate_projection = _assistant_message(chat_session.id, "msg_assistant_second")
        duplicate_projection.metadata_json = {"user_message_id": user_message.id}
        event = ChannelInboundEvent(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            channel=binding.channel,
            event_id="event_turn_1",
            target_json={"to_user_id": "u1", "context_token": "ctx_turn_1"},
        )
        db.add(chat_session)
        db.add(user_message)
        db.add(first)
        db.add(duplicate_projection)
        db.add(event)
        db.commit()

        stage_channel_delivery(db, chat_session, first)
        stage_channel_delivery(db, chat_session, duplicate_projection)
        db.commit()

        deliveries = db.exec(select(ChannelDelivery)).all()
        assert len(deliveries) == 1
        assert deliveries[0].idempotency_key == f"channel-reply:{binding.id}:event_turn_1"
        assert deliveries[0].target_json["context_token"] == "ctx_turn_1"


def test_channel_staging_failure_propagates() -> None:
    class BrokenDb:
        def exec(self, _statement):
            raise RuntimeError("db 炸了")

    chat_session = ChatSession(
        id="session_chan",
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel="fake",
        channel_target_json={"to_user_id": "u1", "context_token": "ctx"},
        channel_account_key="fake:account",
    )
    message = _assistant_message("session_chan", "msg_x")
    with pytest.raises(RuntimeError, match="db 炸了"):
        stage_channel_delivery(BrokenDb(), chat_session, message)


def test_legacy_session_claim_conflict_fails_channel_transaction() -> None:
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

        with pytest.raises(RuntimeError, match="认领冲突"):
            stage_channel_delivery(db, legacy, message)
        db.rollback()

        db.refresh(legacy)
        assert legacy.channel_binding_id is None
        assert db.exec(select(ChannelDelivery)).all() == []
        assert db.get(Message, message.id).content == "回复内容"


def test_missing_target_stages_failed_delivery_for_audit() -> None:
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
        delivery = db.exec(select(ChannelDelivery)).one()
        assert delivery.status == "failed"
        assert delivery.last_error == "delivery_target_missing"
        assert delivery.next_attempt_at is None


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
        assert delivery.status == "failed"
        assert delivery.last_error == "remote_state_unknown"
        assert delivery.attempts == 3
        assert adapter.sent == []


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


def _seed_feishu_reaction_event(db: Session, binding: ChannelBinding) -> ChannelInboundEvent:
    event = ChannelInboundEvent(
        id="event_feishu",
        tenant_id=binding.tenant_id,
        binding_id=binding.id,
        channel="feishu",
        event_id="om_feishu",
        target_json={"message_id": "om_feishu", "receive_id": "ou_user"},
        status="done",
    )
    db.add(event)
    db.commit()
    return event


def _seed_feishu_delivery(
    db: Session,
    binding: ChannelBinding,
    event: ChannelInboundEvent,
    *,
    kind: str,
    status: str = "pending",
    final: bool = True,
) -> ChannelDelivery:
    target = {"message_id": event.event_id, "event_pk": event.id}
    if kind == "reaction_remove":
        target["reaction_id"] = event.reaction_id
    if final and kind not in {"reaction_add", "reaction_remove"}:
        target["reaction_final"] = True
    delivery = ChannelDelivery(
        tenant_id=binding.tenant_id,
        binding_id=binding.id,
        session_id=f"event:{event.id}",
        target_json=target,
        kind=kind,
        text="Get" if kind == "reaction_add" else "处理完成",
        status=status,
        next_attempt_at=utc_now() if status == "pending" else None,
        delivered_at=utc_now() if status == "delivered" else None,
        idempotency_key=f"test:{kind}",
    )
    db.add(delivery)
    db.commit()
    return delivery


def test_feishu_reaction_is_removed_after_response_delivery(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        event = _seed_feishu_reaction_event(db, binding)
        reaction_add = _seed_feishu_delivery(db, binding, event, kind="reaction_add")
        response = _seed_feishu_delivery(db, binding, event, kind="notice")
        event_id = event.id
        reaction_add_id = reaction_add.id
        response_id = response.id

    run_reaction_delivery_daemon(once=True, db_engine=engine)
    run_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        assert db.get(ChannelInboundEvent, event_id).reaction_id == "reaction_123"
        assert db.get(ChannelDelivery, reaction_add_id).status == "delivered"
        assert db.get(ChannelDelivery, response_id).status == "delivered"
        cleanup = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "reaction_remove")
        ).one()
        assert cleanup.status == "pending"

    run_reaction_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        assert db.get(ChannelInboundEvent, event_id).reaction_id is None
        cleanup = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "reaction_remove")
        ).one()
        assert cleanup.status == "delivered"
    assert adapter.calls == [
        ("add", "om_feishu", "Get"),
        ("send", "om_feishu", "处理完成"),
        ("remove", "om_feishu", "reaction_123"),
    ]


def test_delayed_feishu_reaction_is_cleaned_after_existing_response(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        event = _seed_feishu_reaction_event(db, binding)
        _seed_feishu_delivery(db, binding, event, kind="notice", status="delivered")
        reaction_add = _seed_feishu_delivery(db, binding, event, kind="reaction_add")
        event_id = event.id
        reaction_add_id = reaction_add.id

    run_reaction_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        assert db.get(ChannelInboundEvent, event_id).reaction_id == "reaction_123"
        assert db.get(ChannelDelivery, reaction_add_id).status == "delivered"
        cleanup = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "reaction_remove")
        ).one()
        assert cleanup.status == "pending"

    run_reaction_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        assert db.get(ChannelInboundEvent, event_id).reaction_id is None
    assert adapter.calls == [
        ("add", "om_feishu", "Get"),
        ("remove", "om_feishu", "reaction_123"),
    ]


def test_feishu_reaction_failure_does_not_block_response(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter(fail_reaction_add=True)
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    settings = get_settings().model_copy(update={"channel_delivery_max_attempts": 1})
    monkeypatch.setattr("app.channels.service_outbox.get_settings", lambda: settings)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        event = _seed_feishu_reaction_event(db, binding)
        reaction_add = _seed_feishu_delivery(db, binding, event, kind="reaction_add")
        response = _seed_feishu_delivery(db, binding, event, kind="notice")
        reaction_add_id = reaction_add.id
        response_id = response.id

    run_reaction_delivery_daemon(once=True, db_engine=engine)
    run_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        failed_reaction = db.get(ChannelDelivery, reaction_add_id)
        delivered_response = db.get(ChannelDelivery, response_id)
        assert failed_reaction.status == "failed"
        assert failed_reaction.last_error == "reaction unavailable"
        assert delivered_response.status == "delivered"
        assert db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "reaction_remove")
        ).all() == []
    assert adapter.calls == [
        ("add", "om_feishu", "Get"),
        ("send", "om_feishu", "处理完成"),
    ]


def test_content_lane_does_not_wait_for_pending_reaction(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        event = _seed_feishu_reaction_event(db, binding)
        reaction_add = _seed_feishu_delivery(db, binding, event, kind="reaction_add")
        response = _seed_feishu_delivery(db, binding, event, kind="notice")
        reaction_add_id = reaction_add.id
        response_id = response.id

    run_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        assert db.get(ChannelDelivery, reaction_add_id).status == "pending"
        assert db.get(ChannelDelivery, response_id).status == "delivered"
    assert adapter.calls == [("send", "om_feishu", "处理完成")]


def test_reaction_retry_recovers_remote_reaction_before_readding(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter(recovered_reaction_id="reaction_recovered")
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        event = _seed_feishu_reaction_event(db, binding)
        reaction_add = _seed_feishu_delivery(db, binding, event, kind="reaction_add")
        reaction_add.status = "sending"
        reaction_add.attempts = 1
        reaction_add.first_attempt_at = utc_now()
        db.add(reaction_add)
        db.commit()
        event_id = event.id

    run_reaction_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        event = db.get(ChannelInboundEvent, event_id)
        assert event.reaction_id == "reaction_recovered"
        delivery = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "reaction_add")
        ).one()
        assert delivery.status == "delivered"
        assert delivery.attempts == 2
    assert adapter.calls == [("find", "om_feishu", "Get")]


def test_intermediate_notice_does_not_remove_reaction(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        event = _seed_feishu_reaction_event(db, binding)
        event.reaction_id = "reaction_123"
        db.add(event)
        db.commit()
        _seed_feishu_delivery(db, binding, event, kind="notice", final=False)
        event_id = event.id

    run_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        assert db.get(ChannelInboundEvent, event_id).reaction_id == "reaction_123"
        assert db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "reaction_remove")
        ).all() == []
        binding = db.exec(select(ChannelBinding).where(ChannelBinding.channel == "feishu")).one()
        event = db.get(ChannelInboundEvent, event_id)
        _seed_feishu_delivery(db, binding, event, kind="error_notice")

    run_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        cleanup = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "reaction_remove")
        ).one()
        assert cleanup.status == "pending"


def test_disabled_feishu_binding_can_finish_reaction_cleanup(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        binding.credentials_enc = "encrypted-secret"
        event = _seed_feishu_reaction_event(db, binding)
        event.reaction_id = "reaction_123"
        db.add(event)
        db.commit()
        cleanup = _seed_feishu_delivery(db, binding, event, kind="reaction_remove")
        binding.status = "disabled"
        db.add(binding)
        db.commit()
        event_id = event.id
        cleanup_id = cleanup.id

    run_reaction_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        assert db.get(ChannelInboundEvent, event_id).reaction_id is None
        assert db.get(ChannelDelivery, cleanup_id).status == "delivered"
    assert adapter.calls == [("remove", "om_feishu", "reaction_123")]


def test_disabled_binding_reconciles_ambiguous_reaction_without_readding(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter(recovered_reaction_id="reaction_recovered")
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        binding.credentials_enc = "encrypted-secret"
        event = _seed_feishu_reaction_event(db, binding)
        reaction_add = _seed_feishu_delivery(db, binding, event, kind="reaction_add")
        reaction_add.attempts = 1
        binding.status = "disabled"
        db.add(reaction_add)
        db.add(binding)
        db.commit()
        event_id = event.id

    run_reaction_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        event = db.get(ChannelInboundEvent, event_id)
        assert event.reaction_id == "reaction_recovered"
        cleanup = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "reaction_remove")
        ).one()
        assert cleanup.status == "pending"

    run_reaction_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        assert db.get(ChannelInboundEvent, event_id).reaction_id is None
    assert adapter.calls == [
        ("find", "om_feishu", "Get"),
        ("remove", "om_feishu", "reaction_recovered"),
    ]


def test_disabled_binding_does_not_start_a_fresh_reaction_add(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        binding.credentials_enc = "encrypted-secret"
        event = _seed_feishu_reaction_event(db, binding)
        delivery = _seed_feishu_delivery(db, binding, event, kind="reaction_add")
        binding.status = "disabled"
        db.add(binding)
        db.commit()
        delivery_id = delivery.id

    run_reaction_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "failed"
        assert delivery.attempts == 1
    assert adapter.calls == []


def test_binding_delete_cleanup_removes_known_feishu_reactions(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        event = _seed_feishu_reaction_event(db, binding)
        event.reaction_id = "reaction_123"
        db.add(event)
        db.commit()

        cleanup_channel_reactions_before_binding_delete(db, binding)
        db.commit()

        assert db.get(ChannelInboundEvent, event.id).reaction_id is None
    assert adapter.calls == [("remove", "om_feishu", "reaction_123")]


@pytest.mark.parametrize("status", ["pending", "sending", "failed"])
def test_binding_delete_cleanup_reconciles_unknown_reaction(
    monkeypatch,
    status: str,
) -> None:
    engine = _test_engine()
    adapter = FakeFeishuAdapter(recovered_reaction_id="reaction_recovered")
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="feishu")
        event = _seed_feishu_reaction_event(db, binding)
        reaction_add = _seed_feishu_delivery(db, binding, event, kind="reaction_add")
        reaction_add.status = status
        reaction_add.attempts = 1
        db.add(reaction_add)
        db.commit()

        cleanup_channel_reactions_before_binding_delete(db, binding)
    assert adapter.calls == [
        ("find", "om_feishu", "Get"),
        ("remove", "om_feishu", "reaction_recovered"),
    ]


class FakeDingTalkAdapter(FakeAdapter):
    """钉钉没有"查询我加过的表情"接口。

    这个假适配器刻意不提供 find_own_reaction，用来固定"声明重挂幂等的渠道不得走
    回查路径"这条契约——否则 outbox 会抛"不支持 reaction 恢复"。
    """

    reaction_token = "🤔思考中"
    reaction_attach_idempotent = True

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str, str]] = []

    def add_reaction(self, binding: ChannelBinding, target: dict, token: str) -> str:
        self.calls.append(("add", str(target.get("message_id") or ""), token))
        return "emotion:2659900"

    def remove_reaction(self, binding: ChannelBinding, target: dict, handle: str) -> None:
        self.calls.append(("remove", str(target.get("message_id") or ""), handle))


def _seed_dingtalk_reaction_event(db: Session, binding: ChannelBinding) -> ChannelInboundEvent:
    event = ChannelInboundEvent(
        id="event_dingtalk",
        tenant_id=binding.tenant_id,
        binding_id=binding.id,
        channel="dingtalk",
        event_id="msg_dingtalk",
        target_json={
            "message_id": "msg_dingtalk",
            "conversation_id": "conv_dingtalk",
            "to_user_id": "staff_1",
            "context_token": "https://oapi.dingtalk.com/robot/send?session=x",
        },
        status="done",
    )
    db.add(event)
    db.commit()
    return event


def _seed_dingtalk_delivery(
    db: Session,
    binding: ChannelBinding,
    event: ChannelInboundEvent,
    *,
    kind: str,
    status: str = "pending",
) -> ChannelDelivery:
    target = reaction_target(event)
    if kind not in {"reaction_add", "reaction_remove"}:
        target["reaction_final"] = True
    delivery = ChannelDelivery(
        tenant_id=binding.tenant_id,
        binding_id=binding.id,
        session_id=f"event:{event.id}",
        target_json=target,
        kind=kind,
        text="🤔思考中" if kind == "reaction_add" else "处理完成",
        status=status,
        next_attempt_at=utc_now() if status == "pending" else None,
        idempotency_key=f"test:{kind}",
    )
    db.add(delivery)
    db.commit()
    return delivery


def test_dingtalk_reaction_is_recalled_after_response_delivery(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeDingTalkAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "dingtalk", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="dingtalk")
        event = _seed_dingtalk_reaction_event(db, binding)
        _seed_dingtalk_delivery(db, binding, event, kind="reaction_add")
        _seed_dingtalk_delivery(db, binding, event, kind="notice")
        event_id = event.id

    run_reaction_delivery_daemon(once=True, db_engine=engine)
    with Session(engine) as db:
        assert db.get(ChannelInboundEvent, event_id).reaction_id == "emotion:2659900"

    # 最终回复送达后才登记撤回，撤回本身走 reaction lane。
    run_delivery_daemon(once=True, db_engine=engine)
    run_reaction_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        assert db.get(ChannelInboundEvent, event_id).reaction_id is None
        removal = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "reaction_remove")
        ).first()
        assert removal.status == "delivered"
        assert removal.idempotency_key.startswith("dingtalk-reaction-remove:")
        # 撤回目标必须带上 openConversationId，否则钉钉 emotion 接口定位不到消息。
        assert removal.target_json["conversation_id"] == "conv_dingtalk"
    assert adapter.calls == [
        ("add", "msg_dingtalk", "🤔思考中"),
        ("remove", "msg_dingtalk", "emotion:2659900"),
    ]


def test_dingtalk_reaction_retry_reattaches_without_query(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeDingTalkAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "dingtalk", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="dingtalk")
        event = _seed_dingtalk_reaction_event(db, binding)
        delivery = _seed_dingtalk_delivery(db, binding, event, kind="reaction_add")
        delivery.attempts = 1
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id
        event_id = event.id

    run_reaction_delivery_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        assert db.get(ChannelDelivery, delivery_id).status == "delivered"
        assert db.get(ChannelInboundEvent, event_id).reaction_id == "emotion:2659900"
    assert adapter.calls == [("add", "msg_dingtalk", "🤔思考中")]


def test_dingtalk_binding_delete_cleanup_recalls_without_query(monkeypatch) -> None:
    engine = _test_engine()
    adapter = FakeDingTalkAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "dingtalk", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db, channel="dingtalk")
        event = _seed_dingtalk_reaction_event(db, binding)
        uncertain = _seed_dingtalk_delivery(
            db, binding, event, kind="reaction_add", status="failed"
        )
        uncertain.attempts = 1
        db.add(uncertain)
        db.commit()

        cleanup_channel_reactions_before_binding_delete(db, binding)

    # 挂没挂上无从查证，只能按与挂上对称的参数无条件撤回。
    assert adapter.calls == [("remove", "msg_dingtalk", "")]


def test_two_workers_atomically_claim_one_delivery(tmp_path) -> None:
    import app.channels.service_outbox as outbox

    engine = create_engine(
        f"sqlite:///{tmp_path / 'outbox.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    adapter = FakeAdapter()
    register_channel_adapter("fake", adapter)
    with Session(engine) as db:
        binding = _seed_binding(db)
        chat_session = _channel_session(binding)
        delivery = ChannelDelivery(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            session_id=chat_session.id,
            message_id="msg_atomic",
            target_json=dict(chat_session.channel_target_json or {}),
            kind="reply",
            text="only once",
            status="pending",
            next_attempt_at=utc_now(),
            idempotency_key="msg_atomic",
        )
        db.add(chat_session)
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id

    barrier = threading.Barrier(2)
    claimed: list[bool] = []

    def worker() -> None:
        with Session(engine) as db:
            barrier.wait(timeout=5.0)
            delivery = outbox._claim_delivery(
                db,
                delivery_id,
                now=utc_now(),
                reaction_lane=False,
            )
            claimed.append(delivery is not None)
            if delivery:
                outbox._deliver_one(db, delivery)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(claimed) == [False, True]
    assert len(adapter.sent) == 1
    with Session(engine) as db:
        delivery = db.get(ChannelDelivery, delivery_id)
        assert delivery.status == "delivered"
        assert delivery.attempts == 1


@pytest.fixture(autouse=True)
def _clean_adapter_registry():
    yield
    from app.channels.adapters.base import _adapters

    _adapters.pop("fake", None)
    _adapters.pop("unknown_channel", None)


# ---------- 原子 claim 与确定性幂等 ----------


def test_concurrent_daemons_claim_disjoint_deliveries(tmp_path) -> None:
    import threading

    import app.channels.service_outbox as outbox

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
        mine: set[str] = set()
        with Session(engine) as db:
            for delivery_id in sorted(all_ids):
                if outbox._claim_delivery(db, delivery_id, now=utc_now(), reaction_lane=False):
                    mine.add(delivery_id)
        claimed.append(mine)

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


# ---------- sending 重置陈旧阈值 ----------


def test_reset_stuck_only_resets_stale_sending() -> None:
    from app.channels.service_outbox import SENDING_STALE_SECONDS, _reset_stuck_deliveries

    engine = _test_engine()
    with Session(engine) as db:
        binding = _seed_binding(db)
        fresh = _make_delivery(db, binding, idempotency_key="fresh", status="sending", sending_since=utc_now())
        stale = _make_delivery(
            db,
            binding,
            idempotency_key="stale",
            status="sending",
            sending_since=utc_now() - timedelta(seconds=SENDING_STALE_SECONDS + 60),
        )
        empty = _make_delivery(db, binding, idempotency_key="empty", status="sending", sending_since=None)
        fresh_id, stale_id, empty_id = fresh.id, stale.id, empty.id

        _reset_stuck_deliveries(db)

        # 阈值内的在飞发送不重置(避免交错启动重复投递)
        fresh = db.get(ChannelDelivery, fresh_id)
        assert fresh.status == "sending"
        assert fresh.sending_since is not None
        # 陈旧与空 sending_since 的远端结果不可判定，禁止自动重发。
        for row_id in (stale_id, empty_id):
            row = db.get(ChannelDelivery, row_id)
            assert row.status == "failed"
            assert row.last_error == "remote_state_unknown"
            assert row.next_attempt_at is None


# ---------- 创建者告警会话限定 binding ----------


def test_notify_scopes_session_lookup_to_own_binding() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(User(id="user_web", tenant_id="tenant_demo", username="zhangsan", password_hash="x"))
        binding_a = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wechat",
            status="active",
            created_by_user_id="user_web",
        )
        binding_b = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wechat",
            status="active",
            created_by_user_id="user_web",
        )
        db.add(binding_a)
        db.add(binding_b)
        db.flush()
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wechat",
                external_account_scope="",
                external_user_id="wxid_creator",
                staffdeck_user_id="user_web",
            )
        )
        # 同一创建者在两个微信账号下各有私聊会话(目标地址不同)
        db.add(
            ChatSession(
                id="s_a",
                tenant_id="tenant_demo",
                user_id="user_web",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_p2p_wxid_creator",
                channel_target_json={"to_user_id": "wxid_A", "context_token": "ctx_A"},
                channel_binding_id=binding_a.id,
            )
        )
        db.add(
            ChatSession(
                id="s_b",
                tenant_id="tenant_demo",
                user_id="user_web",
                agent_id="agent_1",
                channel="wechat",
                external_conv_id="wechat_p2p_wxid_creator",
                channel_target_json={"to_user_id": "wxid_B", "context_token": "ctx_B"},
                channel_binding_id=binding_b.id,
            )
        )
        db.commit()

        from app.channels.service_outbox import notify_binding_creator

        notify_binding_creator(db, db.get(ChannelBinding, binding_b.id), "测试告警")
        alert = db.exec(select(ChannelDelivery).where(ChannelDelivery.kind == "admin_alert")).one()
        # 只取本绑定(B)会话的目标,绝不串到 A 账号
        assert alert.target_json == {"to_user_id": "wxid_B", "context_token": "ctx_B"}
        assert alert.session_id == "s_b"


# ---------- 创建者告警身份 fallback 限定 scope ----------


def test_notify_identity_fallback_uses_own_binding_scope() -> None:
    """corpA/corpB 两条身份:从 corpB binding 触发,fallback 目标必须是 corpB 的 external_user_id。"""
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(User(id="user_web", tenant_id="tenant_demo", username="zhangsan", password_hash="x"))
        binding_b = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wecom",
            status="active",
            config_json={"corp_id": "corpB", "bot_id": "bot_b"},
            created_by_user_id="user_web",
        )
        db.add(binding_b)
        db.flush()
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="corpA",
                external_user_id="zhangsan_corp_a",
                staffdeck_user_id="user_web",
            )
        )
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="corpB",
                external_user_id="zhangsan_corp_b",
                staffdeck_user_id="user_web",
            )
        )
        db.commit()

        from app.channels.service_outbox import notify_binding_creator

        # 无会话:走身份 fallback;不得拿 corpA 的 external_user_id 经 corpB 发送
        notify_binding_creator(db, db.get(ChannelBinding, binding_b.id), "测试告警")
        alert = db.exec(select(ChannelDelivery).where(ChannelDelivery.kind == "admin_alert")).one()
        assert alert.target_json["to_user_id"] == "zhangsan_corp_b"
        assert alert.binding_id == binding_b.id


def test_notify_identity_fallback_skips_when_scope_missing() -> None:
    """创建者只有其他 scope 的身份:跳过告警,不跨 scope 投递。"""
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(User(id="user_web", tenant_id="tenant_demo", username="zhangsan", password_hash="x"))
        binding_b = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wecom",
            status="active",
            config_json={"corp_id": "corpB", "bot_id": "bot_b"},
            created_by_user_id="user_web",
        )
        db.add(binding_b)
        db.flush()
        # 创建者只有 corpA 身份,与 corpB binding 的 scope 不匹配
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="wecom",
                external_account_scope="corpA",
                external_user_id="zhangsan_corp_a",
                staffdeck_user_id="user_web",
            )
        )
        db.commit()

        from app.channels.service_outbox import notify_binding_creator

        notify_binding_creator(db, db.get(ChannelBinding, binding_b.id), "测试告警")
        assert db.exec(select(ChannelDelivery)).all() == []
