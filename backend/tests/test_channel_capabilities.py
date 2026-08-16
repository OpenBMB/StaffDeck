"""渠道能力声明协议与投递/入站数据模型地基测试。"""

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.adapters.base import (
    ChannelCapability,
    channel_capabilities_of,
)
from app.db.models import ChannelDelivery, ChannelInboundEvent


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class _CapabilityAdapter:
    """实现了 ChannelCapabilityAdapter 协议的假适配器。"""

    def channel_capabilities(self) -> set[ChannelCapability]:
        return {ChannelCapability.SLASH_COMMANDS, ChannelCapability.THREADS}


class _PlainAdapter:
    """只有 send 的存量适配器,未实现能力声明协议。"""

    def send(self, *args, **kwargs) -> None:
        return None


class _PropertyAdapter:
    """channel_capabilities 是属性而非方法的错误实现,应安全降级。"""

    @property
    def channel_capabilities(self) -> set[ChannelCapability]:  # type: ignore[return]
        return {ChannelCapability.VOICE}


def test_channel_capabilities_of_declared_adapter() -> None:
    adapter = _CapabilityAdapter()
    assert channel_capabilities_of(adapter) == {
        ChannelCapability.SLASH_COMMANDS,
        ChannelCapability.THREADS,
    }


def test_channel_capabilities_of_plain_adapter_returns_empty() -> None:
    """核心向后兼容断言:未实现协议的存量适配器自动降级为空集。"""
    assert channel_capabilities_of(_PlainAdapter()) == set()


def test_channel_capabilities_of_property_adapter_returns_empty() -> None:
    assert channel_capabilities_of(_PropertyAdapter()) == set()


def test_channel_capabilities_of_none_returns_empty() -> None:
    assert channel_capabilities_of(None) == set()


def test_channel_capability_values() -> None:
    assert ChannelCapability.SLASH_COMMANDS == "slash_commands"
    assert ChannelCapability.THREADS == "threads"
    assert ChannelCapability.BATCH_SEND == "batch_send"
    assert ChannelCapability.BACKFILL == "backfill"
    assert ChannelCapability.TYPING == "typing"
    assert ChannelCapability.VOICE == "voice"
    assert ChannelCapability.RICH_MEDIA == "rich_media"


def test_channel_delivery_new_fields_roundtrip() -> None:
    engine = _test_engine()
    delivery = ChannelDelivery(
        tenant_id="tenant_demo",
        binding_id="binding_1",
        session_id="session_1",
        text="hello",
        payload_json='{"embeds": [{"title": "t"}]}',
        thread_id="thread_123",
        batch_id="batch_42",
        delivery_kind="voice",
        idempotency_key="idem_1",
    )
    with Session(engine) as db:
        db.add(delivery)
        db.commit()
    with Session(engine) as db:
        stored = db.exec(select(ChannelDelivery)).first()
        assert stored is not None
        assert stored.payload_json == '{"embeds": [{"title": "t"}]}'
        assert stored.thread_id == "thread_123"
        assert stored.batch_id == "batch_42"
        assert stored.delivery_kind == "voice"


def test_channel_delivery_new_fields_default_null() -> None:
    """新字段默认 None,存量行为(纯文本投递)不受影响。"""
    engine = _test_engine()
    delivery = ChannelDelivery(
        tenant_id="tenant_demo",
        binding_id="binding_1",
        session_id="session_1",
        text="plain",
        idempotency_key="idem_2",
    )
    with Session(engine) as db:
        db.add(delivery)
        db.commit()
    with Session(engine) as db:
        stored = db.exec(select(ChannelDelivery)).first()
        assert stored is not None
        assert stored.payload_json is None
        assert stored.thread_id is None
        assert stored.batch_id is None
        assert stored.delivery_kind is None


def test_channel_inbound_event_new_fields_roundtrip() -> None:
    engine = _test_engine()
    event = ChannelInboundEvent(
        tenant_id="tenant_demo",
        binding_id="binding_1",
        channel="discord",
        event_id="evt_1",
        thread_id="thread_123",
        mention_user_ids='["user_a", "user_b"]',
        command="/employee",
    )
    with Session(engine) as db:
        db.add(event)
        db.commit()
    with Session(engine) as db:
        stored = db.exec(select(ChannelInboundEvent)).first()
        assert stored is not None
        assert stored.thread_id == "thread_123"
        assert stored.mention_user_ids == '["user_a", "user_b"]'
        assert stored.command == "/employee"


def test_channel_inbound_event_new_fields_default_null() -> None:
    """文本指令入站事件不填 command,存量字段不受影响。"""
    engine = _test_engine()
    event = ChannelInboundEvent(
        tenant_id="tenant_demo",
        binding_id="binding_1",
        channel="wechat",
        event_id="evt_2",
    )
    with Session(engine) as db:
        db.add(event)
        db.commit()
    with Session(engine) as db:
        stored = db.exec(select(ChannelInboundEvent)).first()
        assert stored is not None
        assert stored.thread_id is None
        assert stored.mention_user_ids is None
        assert stored.command is None
