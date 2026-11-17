import threading

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.execution_failure import blocked_handoff, stop_execution
from app.db.models import AgentEvent, AgentProfile, ChatSession, HumanHandoffRequest, User
from staffdeck_harness.capabilities.host import CapabilityHost
from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation, ModuleResult


@pytest.fixture
def db(monkeypatch):
    from staffdeck_harness.security.oss_local import build_oss_local_profile
    monkeypatch.setattr("staffdeck_harness.modules.registry._active", None)
    monkeypatch.setattr("staffdeck_harness.security.profile._active", build_oss_local_profile())
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            User(id="owner", tenant_id="t", username="owner", password_hash="test"),
            User(id="admin", tenant_id="t", username="admin", role="admin", password_hash="test"),
            AgentProfile(id="a", tenant_id="t", name="Agent", metadata_json={"owner_user_id": "owner"}),
            ChatSession(id="s", tenant_id="t", agent_id="a", user_id="owner"),
        ])
        session.commit()
        yield session
    engine.dispose()


@pytest.mark.parametrize("has_owner,expected", [(True, "owner"), (False, "admin")])
def test_owner_then_admin_and_web_only(db, has_owner, expected):
    if not has_owner:
        db.get(AgentProfile, "a").metadata_json = {}
    row = stop_execution(db, db.get(ChatSession, "s"), "frame", operation="remote", reason="offline")
    db.commit()
    assert row.assignee_user_id == expected
    assert row.metadata_json["assignee_notify_channel"] == "web"
    assert db.get(ChatSession, "s").status == "handoff"
    events = db.exec(select(AgentEvent).where(AgentEvent.event_type == "human_handoff_notified")).all()
    assert len(events) == 1 and events[0].payload_json["channel"] == "web"


def make_host(db, result):
    host = object.__new__(CapabilityHost)
    host.db, host.registry, host._invoke_lock = db, None, threading.RLock()
    host._invoke = lambda inv: (result, None)
    return host


def invocation(operation="tool.invoke/v1", task="frame"):
    return ModuleInvocation("call", "tool", operation, {}, InvocationContext(
        tenant_id="t", agent_id="a", user_id="owner", session_id="s", turn_id="turn",
        channel="web", task_frame_id=task))


@pytest.mark.parametrize("result", [
    ModuleResult.ok({"remote_async_ready": False}),
    ModuleResult.fail("PROVIDER_UNAVAILABLE", "offline"),
    ModuleResult.fail("COMMAND_TIMEOUT", "timeout"),
    ModuleResult.ok({"success": False}),
])
def test_failure_blocks_other_executor_and_survives_new_host(db, result):
    first, _ = make_host(db, result).invoke(invocation())
    assert first.extensions["execution_stopped"]
    new_host = make_host(db, ModuleResult.ok())
    new_host._invoke = lambda inv: pytest.fail("replacement executor must not run")
    denied, _ = new_host.invoke(invocation("sandbox.execute/v1"))
    assert denied.error["code"] == "EXECUTION_REQUIRES_HUMAN"
    assert len(db.exec(select(HumanHandoffRequest)).all()) == 1


def test_schema_correction_and_other_task_are_not_blocked(db):
    host = make_host(db, ModuleResult.fail("INVALID_ARGUMENTS", "schema"))
    host.invoke(invocation())
    assert blocked_handoff(db, "s", "frame") is None
    stop_execution(db, db.get(ChatSession, "s"), "frame", operation="remote", reason="offline")
    db.commit()
    assert make_host(db, ModuleResult.ok()).invoke(invocation(task="other"))[0].success


def test_timeout_keeps_unknown_and_human_reply_allows_resume(db):
    make_host(db, ModuleResult.fail("COMMAND_TIMEOUT", "timeout")).invoke(invocation())
    row = blocked_handoff(db, "s", "frame")
    assert row.metadata_json["outcome_unknown"] is True
    row.status = "answered"
    db.add(row)
    db.commit()
    assert make_host(db, ModuleResult.ok()).invoke(invocation())[0].success


def test_async_failure_creates_handoff_without_chat_notification(db):
    from app.db.models import ExternalBusinessTask
    from app.core.execution_failure import escalate_external_failure
    task = ExternalBusinessTask(tenant_id="t", user_id="owner", agent_id="a", session_id="s",
                               task_frame_id="frame", tool_id="tool", callback_token_hash="test",
                               status="failed", error_json={"message": "remote failed"})
    db.add(task)
    db.commit()
    escalate_external_failure(db, task)
    escalate_external_failure(db, task)
    db.commit()
    assert len(db.exec(select(HumanHandoffRequest)).all()) == 1


def test_directory_outage_preserves_stop_and_reports_notification_failure(db, monkeypatch):
    from staffdeck_harness.contracts.errors import ModuleSdkError

    def unavailable(*args, **kwargs):
        raise ModuleSdkError("directory unavailable", code="SOURCE_UNAVAILABLE")
    monkeypatch.setattr("staffdeck_harness.runtime.staff_ownership.handoff_staff", unavailable)
    row = stop_execution(db, db.get(ChatSession, "s"), "frame", operation="remote", reason="offline")
    db.commit()
    assert blocked_handoff(db, "s", "frame").id == row.id
    assert "routing_error" in row.metadata_json
    assert "未送达" in row.pending_question
    assert not db.exec(select(AgentEvent).where(AgentEvent.event_type == "human_handoff_notified")).all()


def test_unknown_async_receipt_does_not_modify_or_resubmit_original_task(db):
    from app.db.models import ExternalBusinessTask
    from app.core.execution_failure import escalate_external_failure

    task = ExternalBusinessTask(id="original", tenant_id="t", user_id="owner", agent_id="a",
                               session_id="s", task_frame_id="frame", tool_id="tool",
                               callback_token_hash="test", status="outcome_unknown",
                               external_task_id="remote-original")
    db.add(task)
    db.commit()
    escalate_external_failure(db, task)
    db.commit()
    assert task.external_task_id == "remote-original"
    assert task.status == "outcome_unknown"
    assert blocked_handoff(db, "s", "frame").metadata_json["outcome_unknown"]
    assert len(db.exec(select(ExternalBusinessTask)).all()) == 1
