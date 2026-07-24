import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.sessions import get_session_detail, list_sessions, reset_session
from app.db.models import AgentProfile, ChatSession, Tenant, User


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session) -> dict[str, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    admin = User(id="admin_user", tenant_id="tenant_demo", username="admin", role="admin", password_hash="x")
    owner = User(id="owner_user", tenant_id="tenant_demo", username="owner", password_hash="x")
    member = User(id="member_user", tenant_id="tenant_demo", username="member", password_hash="x")
    wechat_user = User(
        id="wechat_user_1",
        tenant_id="tenant_demo",
        username="wechat_u1",
        display_name="微信用户 ab12cd34",
        password_hash="x",
    )
    db.add_all([admin, owner, member, wechat_user])
    db.add(
        AgentProfile(
            id="agent_emp",
            tenant_id="tenant_demo",
            name="客服员工",
            metadata_json={"owner_user_id": owner.id},
        )
    )
    db.add(
        AgentProfile(
            id="agent_overall",
            tenant_id="tenant_demo",
            name="整体智能体",
            is_overall=True,
            metadata_json={},
        )
    )
    db.add_all(
        [
            ChatSession(
                id="session_owner",
                tenant_id="tenant_demo",
                user_id=owner.id,
                agent_id="agent_emp",
                title="owner 的会话",
            ),
            ChatSession(
                id="session_member",
                tenant_id="tenant_demo",
                user_id=member.id,
                agent_id="agent_emp",
                title="member 的会话",
            ),
            ChatSession(
                id="session_channel",
                tenant_id="tenant_demo",
                user_id=wechat_user.id,
                agent_id="agent_emp",
                channel="wechat",
                external_conv_id="wechat_p2p_u1",
                channel_binding_id="chan_1",
                title="渠道会话",
                active_skill_id="skill_x",
                slots_json={"step": "1"},
            ),
            ChatSession(
                id="session_overall",
                tenant_id="tenant_demo",
                user_id=member.id,
                agent_id="agent_overall",
                title="整体员工会话",
            ),
        ]
    )
    db.commit()
    return {"admin": admin, "owner": owner, "member": member, "wechat_user": wechat_user}


def test_agent_creator_sees_all_sessions_of_the_agent() -> None:
    with _test_session() as db:
        users = _seed(db)
        rows = list_sessions("tenant_demo", agent_id="agent_emp", current_user=users["owner"], db=db)
        session_ids = {row["id"] for row in rows}
        assert session_ids == {"session_owner", "session_member", "session_channel"}


def test_admin_sees_all_sessions_with_agent_id() -> None:
    with _test_session() as db:
        users = _seed(db)
        rows = list_sessions("tenant_demo", agent_id="agent_emp", current_user=users["admin"], db=db)
        assert {row["id"] for row in rows} == {"session_owner", "session_member", "session_channel"}


def test_member_only_sees_own_sessions() -> None:
    with _test_session() as db:
        users = _seed(db)
        rows = list_sessions("tenant_demo", agent_id="agent_emp", current_user=users["member"], db=db)
        assert [row["id"] for row in rows] == ["session_member"]

        # 无 agent_id 时 admin 也只看自己
        admin_rows = list_sessions("tenant_demo", agent_id=None, current_user=users["admin"], db=db)
        assert admin_rows == []


def test_overall_agent_never_opens_to_non_admin() -> None:
    with _test_session() as db:
        users = _seed(db)
        # member 即使提供 agent_id 也仅见自己的整体员工会话
        member_rows = list_sessions(
            "tenant_demo", agent_id="agent_overall", current_user=users["member"], db=db
        )
        assert [row["id"] for row in member_rows] == ["session_overall"]
        # owner 不是整体员工创建者(is_overall 创建者永不匹配),没有自己的会话则为空
        owner_rows = list_sessions(
            "tenant_demo", agent_id="agent_overall", current_user=users["owner"], db=db
        )
        assert owner_rows == []
        # admin 可见全部整体员工会话
        admin_rows = list_sessions(
            "tenant_demo", agent_id="agent_overall", current_user=users["admin"], db=db
        )
        assert [row["id"] for row in admin_rows] == ["session_overall"]


def test_detail_allowed_for_owner_admin_and_agent_creator() -> None:
    with _test_session() as db:
        users = _seed(db)
        # 会话属主
        own = get_session_detail("session_member", "tenant_demo", current_user=users["member"], db=db)
        assert own["session"]["id"] == "session_member"
        # admin
        by_admin = get_session_detail("session_member", "tenant_demo", current_user=users["admin"], db=db)
        assert by_admin["session"]["id"] == "session_member"
        # agent 创建者查看渠道会话
        by_creator = get_session_detail("session_channel", "tenant_demo", current_user=users["owner"], db=db)
        assert by_creator["session"]["id"] == "session_channel"


def test_detail_404_for_other_members() -> None:
    with _test_session() as db:
        users = _seed(db)
        with pytest.raises(HTTPException) as exc_info:
            get_session_detail("session_channel", "tenant_demo", current_user=users["member"], db=db)
        assert exc_info.value.status_code == 404
        with pytest.raises(HTTPException) as exc_info:
            get_session_detail("session_owner", "tenant_demo", current_user=users["member"], db=db)
        assert exc_info.value.status_code == 404


def test_reset_allowed_for_agent_creator_and_admin_only() -> None:
    with _test_session() as db:
        users = _seed(db)
        with pytest.raises(HTTPException) as exc_info:
            reset_session("session_channel", "tenant_demo", current_user=users["member"], db=db)
        assert exc_info.value.status_code == 404

        # agent 创建者可重置渠道会话
        payload = reset_session("session_channel", "tenant_demo", current_user=users["owner"], db=db)
        assert payload["id"] == "session_channel"
        assert payload["active_skill_id"] is None
        row = db.get(ChatSession, "session_channel")
        assert row.slots_json == {}
        assert row.status == "active"

        # admin 也可重置
        admin_payload = reset_session("session_member", "tenant_demo", current_user=users["admin"], db=db)
        assert admin_payload["id"] == "session_member"


def test_augment_fields_channel_and_identity() -> None:
    with _test_session() as db:
        users = _seed(db)
        rows = list_sessions("tenant_demo", agent_id="agent_emp", current_user=users["owner"], db=db)
        by_id = {row["id"]: row for row in rows}

        channel_row = by_id["session_channel"]
        assert channel_row["channel"] == "wechat"
        assert channel_row["session_username"] == "wechat_u1"
        assert channel_row["session_display_name"] == "微信用户 ab12cd34"

        web_row = by_id["session_member"]
        assert web_row["channel"] is None
        assert web_row["session_username"] == "member"
        assert web_row["session_display_name"] is None

        # detail 同样带 augment 字段
        detail = get_session_detail("session_channel", "tenant_demo", current_user=users["admin"], db=db)
        assert detail["session"]["channel"] == "wechat"
        assert detail["session"]["session_display_name"] == "微信用户 ab12cd34"
