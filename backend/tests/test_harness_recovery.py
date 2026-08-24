from __future__ import annotations

from datetime import timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.harness_recovery import RECOVERY_REPLY, recover_orphan_harness_runs
from app.db.models import (
    AgentEvent,
    ChatSession,
    HarnessAgentLoopRecord,
    HarnessRunRecord,
    HarnessSessionLeaseRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    Message,
    utc_now,
)


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _add_active_execution(
    db: Session,
    *,
    lease_expires_at,
    session_id: str = "session-orphan",
) -> None:
    session = ChatSession(
        id=session_id,
        tenant_id="tenant-demo",
        status="running",
        active_skill_id="after_sales_refund",
        active_step_id="confirm_refund_order",
        slots_json={"order_id": "ORDER-1"},
    )
    user_message = Message(
        id="msg-user",
        tenant_id=session.tenant_id,
        session_id=session.id,
        role="user",
        content="ORDER-1",
    )
    turn = HarnessTurnRecord(
        id="hturn-orphan",
        tenant_id=session.tenant_id,
        session_id=session.id,
        client_turn_id="client-turn-orphan",
        request_digest="sha256:orphan",
        lease_owner="turn-worker",
        lease_expires_at=lease_expires_at,
        user_message_id=user_message.id,
    )
    loop = HarnessAgentLoopRecord(
        id="hloop-orphan",
        tenant_id=session.tenant_id,
        session_id=session.id,
        loop_key="sop:after_sales_refund",
        kind="sop",
        skill_id="after_sales_refund",
        checkpoint_json={"cursor": "confirm_refund_order"},
    )
    frame = HarnessTaskFrameRecord(
        id="htask-orphan",
        tenant_id=session.tenant_id,
        session_id=session.id,
        source_turn_id=user_message.id,
        task_id="task-orphan",
        agent_loop_id=loop.id,
        kind="sop",
        decision="continue_sop",
        status="running",
        skill_id="after_sales_refund",
        step_id="confirm_refund_order",
        slots_json={"order_id": "ORDER-1"},
        attempt_no=3,
        lease_owner="frame-worker",
        lease_expires_at=lease_expires_at,
    )
    run = HarnessRunRecord(
        id="hrun-orphan",
        tenant_id=session.tenant_id,
        session_id=session.id,
        task_frame_record_id=frame.id,
        agent_loop_id=loop.id,
        task_id=frame.task_id,
        source_turn_id=frame.source_turn_id,
        attempt_no=frame.attempt_no,
        lease_owner=frame.lease_owner,
        lease_expires_at=lease_expires_at,
    )
    lease = HarnessSessionLeaseRecord(
        id="hslease-orphan",
        tenant_id=session.tenant_id,
        session_id=session.id,
        lease_owner="session-worker",
        lease_expires_at=lease_expires_at,
    )
    db.add_all([session, user_message, turn, loop, frame, run, lease])
    db.commit()


def test_startup_recovery_terminalizes_attempt_and_preserves_checkpoint() -> None:
    engine = _engine()
    now = utc_now()
    with Session(engine) as db:
        _add_active_execution(db, lease_expires_at=now + timedelta(minutes=10))

        result = recover_orphan_harness_runs(db, startup=True, now=now)

        assert result.run_count == 1
        assert result.frame_count == 1
        assert result.turn_count == 1
        assert result.session_count == 1
        assert result.message_count == 1

        run = db.get(HarnessRunRecord, "hrun-orphan")
        frame = db.get(HarnessTaskFrameRecord, "htask-orphan")
        loop = db.get(HarnessAgentLoopRecord, "hloop-orphan")
        turn = db.get(HarnessTurnRecord, "hturn-orphan")
        session = db.get(ChatSession, "session-orphan")
        assert run is not None and run.status == "abandoned"
        assert run.result_json["error"]["code"] == "SERVICE_RESTARTED"
        assert frame is not None and frame.status == "queued"
        assert frame.step_id == "confirm_refund_order"
        assert frame.slots_json == {"order_id": "ORDER-1"}
        assert loop is not None and loop.status == "suspended"
        assert loop.checkpoint_json == {"cursor": "confirm_refund_order"}
        assert turn is not None and turn.status == "failed"
        assert session is not None and session.status == "active"
        assert db.get(HarnessSessionLeaseRecord, "hslease-orphan") is None

        replies = list(
            db.exec(
                select(Message).where(
                    Message.session_id == "session-orphan",
                    Message.role == "assistant",
                )
            ).all()
        )
        assert [reply.content for reply in replies] == [RECOVERY_REPLY]
        events = list(
            db.exec(
                select(AgentEvent).where(AgentEvent.session_id == "session-orphan")
            ).all()
        )
        assert {event.event_type for event in events} == {
            "assistant_message_created",
            "harness_execution_recovered",
        }

        repeated = recover_orphan_harness_runs(db, startup=True, now=now)
        assert repeated == repeated.__class__()
        replies = list(
            db.exec(
                select(Message).where(
                    Message.session_id == "session-orphan",
                    Message.role == "assistant",
                )
            ).all()
        )
        assert len(replies) == 1


def test_runtime_sweeper_ignores_live_execution() -> None:
    engine = _engine()
    now = utc_now()
    with Session(engine) as db:
        _add_active_execution(db, lease_expires_at=now + timedelta(minutes=10))

        result = recover_orphan_harness_runs(db, now=now)

        assert result == result.__class__()
        assert db.get(HarnessRunRecord, "hrun-orphan").status == "running"
        assert db.get(HarnessTurnRecord, "hturn-orphan").status == "started"
        assert db.get(ChatSession, "session-orphan").status == "running"


def test_runtime_sweeper_recovers_expired_execution() -> None:
    engine = _engine()
    now = utc_now()
    with Session(engine) as db:
        _add_active_execution(db, lease_expires_at=now - timedelta(seconds=1))

        result = recover_orphan_harness_runs(db, now=now)

        assert result.run_count == 1
        assert result.frame_count == 1
        assert result.turn_count == 1
        assert db.get(HarnessRunRecord, "hrun-orphan").status == "abandoned"
        assert db.get(HarnessTaskFrameRecord, "htask-orphan").status == "queued"
        turn = db.get(HarnessTurnRecord, "hturn-orphan")
        assert turn is not None and turn.error_json["code"] == "HARNESS_EXECUTION_LOST"
        assert db.get(ChatSession, "session-orphan").status == "active"
