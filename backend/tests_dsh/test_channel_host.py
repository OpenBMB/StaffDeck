from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.channels.adapters import ChannelInbound
from app.db.models import AgentProfile, ChannelBinding, ChatSession, Message, Tenant, User
from staffdeck_dsh.channels import ChannelHost
from staffdeck_dsh.contracts import PermissionDenied
from staffdeck_dsh.security import build_oss_local_profile


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Tenant(id="t1", name="T"))
        s.add(User(id="alice", tenant_id="t1", username="alice", role="member", password_hash="x", source="feishu"))
        s.add(AgentProfile(id="a1", tenant_id="t1", name="A", status="active", metadata_json={"owner_user_id": "alice"}))
        s.add(AgentProfile(id="a2", tenant_id="t1", name="Private", status="active", metadata_json={"owner_user_id": "someone"}))
        s.add(ChannelBinding(id="cb1", tenant_id="t1", agent_id="a1", channel="feishu", status="active"))
        s.add(ChannelBinding(id="cb2", tenant_id="t1", agent_id="a2", channel="feishu", status="active"))
        s.add(ChannelBinding(id="cb3", tenant_id="t1", agent_id="a1", channel="feishu", status="disabled"))
        s.commit()
        yield s


def _inbound() -> ChannelInbound:
    return ChannelInbound(channel="feishu", event_id="e1", from_user_id="ou_1", to_user_id="bot", session_id="p2p_1", group_id="", context_token="", text="hi", is_group=False, raw={})


def test_receive_denies_unmapped_identity(db) -> None:
    host = ChannelHost(db, build_oss_local_profile())
    d = host.authorize_receive(db.get(ChannelBinding, "cb1"), _inbound(), None)
    assert d.allowed is False and "unmapped" in d.reason


def test_receive_allows_mapped_user_on_visible_staff(db) -> None:
    host = ChannelHost(db, build_oss_local_profile())
    d = host.authorize_receive(db.get(ChannelBinding, "cb1"), _inbound(), db.get(User, "alice"))
    assert d.allowed and d.security_context is not None and d.security_context.channel == "feishu"


def test_receive_denies_staff_user_cannot_use(db) -> None:
    host = ChannelHost(db, build_oss_local_profile())
    d = host.authorize_receive(db.get(ChannelBinding, "cb2"), _inbound(), db.get(User, "alice"))
    assert d.allowed is False and "staff" in d.reason


def test_receive_denies_disabled_binding(db) -> None:
    host = ChannelHost(db, build_oss_local_profile())
    d = host.authorize_receive(db.get(ChannelBinding, "cb3"), _inbound(), db.get(User, "alice"))
    assert d.allowed is False and "inactive" in d.reason


def test_send_requires_supervision_and_active_binding(db) -> None:
    host = ChannelHost(db, build_oss_local_profile())
    session = ChatSession(id="s1", tenant_id="t1", user_id="alice", agent_id="a1", channel="feishu", channel_binding_id="cb3", status="active")
    db.add(session)
    db.commit()
    msg = Message(tenant_id="t1", session_id="s1", role="assistant", content="raw")
    with pytest.raises(PermissionDenied):
        host.stage_send(session, msg, supervised=False)
    with pytest.raises(PermissionDenied):
        host.stage_send(session, msg, supervised=True)  # cb3 is disabled → send PEP denies
