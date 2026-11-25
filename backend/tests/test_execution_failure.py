"""Guarded asynchronous writes retain their original receipt and require a human reply."""
import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.execution_failure import HANDOFF_CODE, stop_execution
from app.core.handoff_reply_service import apply_handoff_reply
from app.db.models import (
    AgentProfile, ChatSession, ExternalBusinessTask, HarnessTaskFrameRecord, HumanHandoffRequest, Message, User,
)
from app.tools.external_tasks import _prepare_sop_resume


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr("staffdeck_harness.modules.registry._active", None)
    engine=create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            AgentProfile(id="employee", tenant_id="tenant", name="Employee", metadata_json={"owner_user_id":"owner"}),
            User(id="owner", tenant_id="tenant", username="owner", password_hash="fixture"),
            User(id="admin", tenant_id="tenant", username="admin", password_hash="fixture", role="admin"),
            ChatSession(id="chat", tenant_id="tenant", user_id="owner", agent_id="employee",
                        active_skill_id="sop", active_step_id="step"),
            HarnessTaskFrameRecord(id="frame-row", tenant_id="tenant", session_id="chat",
                task_id="frame", source_turn_id="turn", kind="sop", status="waiting_external_task",
                result_json={"structured_result":{"task_id":"task"}}, skill_id="sop", step_id="step"),
        ])
        session.commit()
        yield session


def test_async_unknown_stays_stopped_after_manual_reply(db, monkeypatch):
    task = ExternalBusinessTask(
        id="task", tenant_id="tenant", user_id="owner", agent_id="employee", session_id="chat",
        task_frame_id="frame", tool_id="submit", external_task_id="original-receipt",
        status="outcome_unknown", callback_token_hash="fixture",
        status_config_json={"_runtime":{"guarded_write":True, "sop_id":"sop", "node_id":"step"}},
    )
    db.add(task)
    db.commit()
    _prepare_sop_resume(db, task)
    _prepare_sop_resume(db, task)
    handoff = db.exec(select(HumanHandoffRequest)).one()
    assert handoff.assignee_user_id == "owner"
    assert db.get(ChatSession, "chat").status == "handoff"
    frame = db.get(HarnessTaskFrameRecord, "frame-row")
    assert frame.status == "handoff" and frame.error_json["code"] == HANDOFF_CODE
    assert task.status == "outcome_unknown" and task.external_task_id == "original-receipt"

    monkeypatch.setattr("staffdeck_harness.handoff.core.authorize_reply", lambda db, row, text, **kw: text)
    apply_handoff_reply(db, handoff, "外部已核对", answered_by_user_id="owner",
        resume=lambda _: pytest.fail("reconciliation must not rerun the business operation"))
    assert handoff.status == "answered" and frame.status == "cancelled"
    assert db.get(ChatSession, "chat").active_skill_id is None
    with pytest.raises(HTTPException) as duplicate:
        apply_handoff_reply(db, handoff, "重复回复", answered_by_user_id="owner")
    assert duplicate.value.status_code == 409
    assert db.exec(select(Message)).one().content == "外部已核对"
    assert task.status == "outcome_unknown" and task.external_task_id == "original-receipt"


def test_only_an_absent_owner_falls_back_to_a_tenant_admin(db):
    employee = db.get(AgentProfile, "employee")
    employee.metadata_json = {}
    session = db.get(ChatSession, "chat")
    row = stop_execution(db, session, "frame", operation="tool.invoke/v1", sop_id="sop")
    assert row.assignee_user_id == "admin"
