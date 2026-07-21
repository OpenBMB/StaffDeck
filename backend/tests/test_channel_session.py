import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, text

from app.channels.service_session import find_or_create_channel_session
from app.db.models import ChannelBinding, ChatSession, Tenant, User


def _test_engine(with_unique_index: bool = False):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    if with_unique_index:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_sessions_agent_channel_extconv "
                    "ON sessions(agent_id, channel, channel_binding_id, external_conv_id)"
                )
            )
    return engine


def _seed(db: Session) -> tuple[ChannelBinding, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    user = User(tenant_id="tenant_demo", username="wechat_wxid_1", password_hash="x")
    binding = ChannelBinding(
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel="wechat",
        status="active",
    )
    db.add(user)
    db.add(binding)
    db.commit()
    return binding, user


def test_create_then_reuse_channel_session() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        binding, user = _seed(db)
        first = find_or_create_channel_session(db, binding, user, "agent_1", "wechat_p2p_wxid_1", "你好，帮我查下订单")
        db.commit()

        assert first.channel == "wechat"
        assert first.external_conv_id == "wechat_p2p_wxid_1"
        assert first.agent_id == "agent_1"
        assert first.user_id == user.id
        assert first.title == "你好，帮我查下订单"
        assert first.channel_binding_id == binding.id

        second = find_or_create_channel_session(db, binding, user, "agent_1", "wechat_p2p_wxid_1", "另一条消息")
        db.commit()
        assert second.id == first.id
        # 复用时标题不被覆盖
        assert second.title == "你好，帮我查下订单"


def test_title_truncated_to_20_chars() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        binding, user = _seed(db)
        chat_session = find_or_create_channel_session(db, binding, user, "agent_1", "wechat_p2p_wxid_1", "x" * 50)
        db.commit()
        assert chat_session.title == "x" * 20


def test_different_conversations_get_different_sessions() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        binding, user = _seed(db)
        one = find_or_create_channel_session(db, binding, user, "agent_1", "wechat_p2p_a", "甲")
        two = find_or_create_channel_session(db, binding, user, "agent_1", "wechat_p2p_b", "乙")
        db.commit()
        assert one.id != two.id


def test_channel_target_json_roundtrip() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        binding, user = _seed(db)
        chat_session = find_or_create_channel_session(db, binding, user, "agent_1", "wechat_p2p_wxid_1", "hi")
        chat_session.channel_target_json = {"to_user_id": "wxid_1", "context_token": "ctx"}
        db.add(chat_session)
        db.commit()

        loaded = db.get(ChatSession, chat_session.id)
        assert loaded.channel_target_json == {"to_user_id": "wxid_1", "context_token": "ctx"}


def test_unique_index_rejects_duplicate_anchor() -> None:
    engine = _test_engine(with_unique_index=True)
    with Session(engine) as db:
        binding, user = _seed(db)
        first = find_or_create_channel_session(db, binding, user, "agent_1", "wechat_p2p_wxid_1", "hi")
        db.commit()

        db.add(
            ChatSession(
                id="session_dup",
                tenant_id="tenant_demo",
                user_id=user.id,
                agent_id=binding.agent_id,
                channel="wechat",
                external_conv_id="wechat_p2p_wxid_1",
                channel_binding_id=binding.id,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.get(ChatSession, first.id) is not None


def test_web_sessions_coexist_under_unique_index() -> None:
    engine = _test_engine(with_unique_index=True)
    with Session(engine) as db:
        _seed(db)
        # channel 为 NULL 的 web 会话在唯一索引下互不相等,可重复存在
        for session_id in ("web_1", "web_2"):
            db.add(ChatSession(id=session_id, tenant_id="tenant_demo", agent_id="agent_1"))
        db.commit()
