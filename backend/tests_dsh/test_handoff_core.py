from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import AgentEvent, AgentProfile, ChatSession, Tenant, User
from staffdeck_dsh.contracts import PermissionDenied, SecurityContext
from staffdeck_dsh.handoff import HandoffCore, HandoffTransitionError, build_handoff_core
from staffdeck_dsh.security import Guard, build_oss_local_profile


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Tenant(id="t1", name="T"))
        s.add(User(id="admin", tenant_id="t1", username="admin", role="admin", password_hash="x"))
        s.add(User(id="owner", tenant_id="t1", username="owner", role="member", password_hash="x"))
        s.add(User(id="alice", tenant_id="t1", username="alice", role="member", password_hash="x"))
        s.add(User(id="bob", tenant_id="t1", username="bob", role="member", password_hash="x"))
        s.add(AgentProfile(id="a1", tenant_id="t1", name="A", status="active", metadata_json={"owner_user_id": "owner"}))
        s.add(ChatSession(id="s1", tenant_id="t1", user_id="alice", agent_id="a1", status="active"))
        s.commit()
        yield s


def _core(db) -> HandoffCore:
    profile = build_oss_local_profile()
    core = build_handoff_core(db, Guard("handoff", profile), channels=())
    applied: list[tuple[str, str, str | None]] = []

    def fake_apply(dbx, row, text, *, answered_by_user_id, source):
        row.status = "answered"
        row.human_reply = text
        dbx.add(row)
        dbx.commit()
        applied.append((row.id, text, answered_by_user_id))

    core.apply_reply = fake_apply
    core._applied = applied  # type: ignore[attr-defined]
    return core


def _ctx(uid: str, role: str = "member") -> SecurityContext:
    return SecurityContext(principal_id=uid, tenant_id="t1", tenant_role=role)  # type: ignore[arg-type]


def test_create_assigns_agent_owner_and_notifies_web(db) -> None:
    core = _core(db)
    session = db.get(ChatSession, "s1")
    row = core.create(_ctx("alice"), session, pending_question="需要人工确认退款", context_summary="用户申请退款")
    assert row.status == "assigned" and row.assignee_user_id == "owner"
    assert db.get(ChatSession, "s1").status == "handoff"
    kinds = [e.event_type for e in db.exec(select(AgentEvent)).all()]
    assert "human_handoff_created" in kinds and "human_handoff_assigned" in kinds and "human_handoff_notified" in kinds


def test_create_is_idempotent_while_open(db) -> None:
    core = _core(db)
    session = db.get(ChatSession, "s1")
    a = core.create(_ctx("alice"), session, pending_question="q1", context_summary="c")
    b = core.create(_ctx("alice"), session, pending_question="q2", context_summary="c")
    assert a.id == b.id


def test_reply_requires_participant_and_creates_new_turn_via_legacy_path(db) -> None:
    core = _core(db)
    session = db.get(ChatSession, "s1")
    row = core.create(_ctx("alice"), session, pending_question="q", context_summary="c")
    with pytest.raises(PermissionDenied):
        core.reply(_ctx("bob"), "web", {"handoff_id": row.id, "reply": "我来处理"}, source="web")
    out = core.reply(_ctx("owner"), "web", {"handoff_id": row.id, "reply": "已处理，可以退款"}, source="web")
    assert out is not None and out.status == "answered"
    assert core._applied == [(row.id, "已处理，可以退款", "owner")]  # type: ignore[attr-defined]
    with pytest.raises(HandoffTransitionError):
        core.reply(_ctx("owner"), "web", {"handoff_id": row.id, "reply": "again"}, source="web")


def test_state_machine_rejects_illegal_transitions(db) -> None:
    core = _core(db)
    session = db.get(ChatSession, "s1")
    row = core.create(_ctx("alice"), session, pending_question="q", context_summary="c")
    with pytest.raises(HandoffTransitionError):
        core.mark_resumed(row)  # assigned → resumed is illegal
    core.cancel(_ctx("admin", "admin"), row)
    assert row.status == "cancelled" and db.get(ChatSession, "s1").status == "active"
    with pytest.raises(HandoffTransitionError):
        core.close(_ctx("admin", "admin"), row)


def test_assign_rejects_foreign_tenant_user(db) -> None:
    core = _core(db)
    db.add(User(id="ext", tenant_id="t2", username="ext", role="member", password_hash="x"))
    db.commit()
    session = db.get(ChatSession, "s1")
    row = core.create(_ctx("alice"), session, pending_question="q", context_summary="c")
    with pytest.raises(HandoffTransitionError):
        core.assign(_ctx("admin", "admin"), row, assignee_user_id="ext")


def test_channel_command_resolver_parses_reply_command(db) -> None:
    from staffdeck_dsh.handoff import ChannelCommandReplyResolver

    core = _core(db)
    session = db.get(ChatSession, "s1")
    row = core.create(_ctx("alice"), session, pending_question="q", context_summary="c")
    resolved = ChannelCommandReplyResolver().resolve(db, "t1", {"text": f"/回复反馈 {row.id} 好的，同意退款"})
    assert resolved == (row.id, "好的，同意退款")
    row.notify_message_id = "om_123"
    db.add(row)
    db.commit()
    assert ChannelCommandReplyResolver().resolve(db, "t1", {"quoted_message_id": "om_123", "text": "同意"}) == (row.id, "同意")
