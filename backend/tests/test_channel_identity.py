from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.service_identity import (
    channel_username,
    external_identity_for_message,
    resolve_or_provision_user,
)
from app.db.models import ChannelIdentity, Tenant, User


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_tenant(db: Session) -> None:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.commit()


def test_external_identity_for_p2p_and_group() -> None:
    external_id, display = external_identity_for_message(
        "wechat", is_group=False, conv_key="", from_user_id="o9cq800kum_ab12cd34@im.wechat"
    )
    assert external_id == "o9cq800kum_ab12cd34@im.wechat"
    assert display == "微信用户 ab12cd34"

    external_id, display = external_identity_for_message(
        "wechat", is_group=True, conv_key="room_123456", from_user_id="sender"
    )
    assert external_id == "group_room_123456"
    assert display == "微信群聊 3456"


def test_provision_creates_member_user_and_identity() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        user = resolve_or_provision_user(db, "tenant_demo", "wechat", "wxid_ab12cd34", "微信用户 ab12cd34")
        db.commit()

        assert user.role == "member"
        assert user.username == channel_username("tenant_demo", "wechat", "wxid_ab12cd34")
        assert user.display_name == "微信用户 ab12cd34"
        assert user.password_hash

        identity = db.exec(
            select(ChannelIdentity).where(
                ChannelIdentity.channel == "wechat",
                ChannelIdentity.external_user_id == "wxid_ab12cd34",
            )
        ).one()
        assert identity.staffdeck_user_id == user.id


def test_second_call_hits_existing_mapping() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        first = resolve_or_provision_user(db, "tenant_demo", "wechat", "wxid_ab12cd34", "微信用户 ab12cd34")
        db.commit()
        second = resolve_or_provision_user(db, "tenant_demo", "wechat", "wxid_ab12cd34", "另一个名字")
        db.commit()

        assert second.id == first.id
        identities = db.exec(
            select(ChannelIdentity).where(ChannelIdentity.external_user_id == "wxid_ab12cd34")
        ).all()
        assert len(identities) == 1


def test_group_account_provision() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        user = resolve_or_provision_user(db, "tenant_demo", "wechat", "group_room_123456", "微信群聊 3456")
        db.commit()

        assert user.username == channel_username("tenant_demo", "wechat", "group_room_123456")
        assert user.display_name == "微信群聊 3456"


def test_username_conflict_falls_back_to_existing_user() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        existing = User(
            tenant_id="tenant_demo",
            username=channel_username("tenant_demo", "wechat", "wxid_conflict"),
            display_name="老账号",
            role="member",
            password_hash="x",
        )
        db.add(existing)
        db.commit()

        user = resolve_or_provision_user(db, "tenant_demo", "wechat", "wxid_conflict", "微信用户 conflict")
        db.commit()

        assert user.id == existing.id
        identity = db.exec(
            select(ChannelIdentity).where(
                ChannelIdentity.channel == "wechat",
                ChannelIdentity.external_user_id == "wxid_conflict",
            )
        ).one()
        assert identity.staffdeck_user_id == existing.id


def test_username_sanitizes_and_truncates() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        long_external = "wx id with space/" + "x" * 100
        user = resolve_or_provision_user(db, "tenant_demo", "wechat", long_external, None)
        db.commit()

        assert " " not in user.username
        assert "/" not in user.username
        assert len(user.username) <= 64


def test_long_external_id_username_uses_stable_hash_suffix() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        # 两个前缀相同、尾部不同的长 userid:截断时代会撞名,hash 后缀后不再冲突
        shared = "corp_user_" + "x" * 55
        id_a = shared + "aaaa"
        id_b = shared + "bbbb"
        user_a = resolve_or_provision_user(db, "tenant_demo", "wecom", id_a, None, "corpX")
        user_b = resolve_or_provision_user(db, "tenant_demo", "wecom", id_b, None, "corpX")
        db.commit()

        assert user_a.id != user_b.id
        assert user_a.username != user_b.username
        assert len(user_a.username) <= 64
        assert len(user_b.username) <= 64
        assert ".." in user_a.username
        # 稳定:同输入同输出
        from app.channels.service_identity import channel_username

        assert channel_username("tenant_demo", "wecom", id_a, "corpX") == user_a.username
        # 短 id 同样带 hash 后缀(本轮语义)
        assert ".." in channel_username("tenant_demo", "wecom", "zhangsan", "corpX")


def test_username_always_carries_stable_hash() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        # 清洗后同形的两个 id,hash 后不再同名,各自建不同 User
        user_a = resolve_or_provision_user(db, "tenant_demo", "wechat", "a/b", None)
        user_b = resolve_or_provision_user(db, "tenant_demo", "wechat", "a_b", None)
        db.commit()
        assert user_a.id != user_b.id
        assert user_a.username != user_b.username
        assert ".." in user_a.username and ".." in user_b.username

        # 不同租户同 id:身份键含 tenant,username 不同
        db.add(Tenant(id="tenant_b", name="B"))
        user_c = resolve_or_provision_user(db, "tenant_b", "wechat", "a/b", None)
        db.commit()
        assert user_c.username != user_a.username

        # 超长 id 仍 ≤64 且带 hash 后缀
        long_user = resolve_or_provision_user(db, "tenant_demo", "wecom", "x" * 100, None, "corpX")
        db.commit()
        assert len(long_user.username) <= 64
        assert ".." in long_user.username
