from types import SimpleNamespace

import pytest
from app.db.models import ChatSession, ScheduledTask, ScheduledTaskRun, utc_now
from app.scheduled_tasks import service
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.runtime import jobs, services, session_binding

BUSINESS_BINDING = {
    "namespace": "business-runtime-tenant",
    "source.staff": "source.staff.business",
    "source.identity": "source.identity.business",
    "security.pep": "security.business",
}


@pytest.mark.parametrize("manual", [False, True])
def test_scheduled_session_is_bound_before_background_execution(monkeypatch, manual):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(
        engine,
        tables=[ScheduledTask.__table__, ScheduledTaskRun.__table__, ChatSession.__table__],
    )
    monkeypatch.setattr(session_binding, "current_binding", lambda _db: BUSINESS_BINDING)
    monkeypatch.setattr(service, "_bind_scheduled_actor", lambda _db, _task: None)
    monkeypatch.setattr(services, "runtime_services", lambda _db: SimpleNamespace(registry=None))
    queued = []

    def enqueue(_key, _callback, task_id, run_id, _manual, **_kwargs):
        # A separate worker transaction must observe the binding before its turn starts.
        with Session(engine) as worker_db:
            run = worker_db.get(ScheduledTaskRun, run_id)
            session = worker_db.get(ChatSession, run.session_id)
            assert session.context_state_json["runtime_binding"] == BUSINESS_BINDING
            session_binding.bind_session(worker_db, session)
            queued.append((task_id, run_id))

    monkeypatch.setattr(jobs, "enqueue_runtime_job", enqueue)
    with Session(engine) as db:
        task = ScheduledTask(
            id="task",
            tenant_id="tenant",
            agent_id="agent",
            created_by_user_id="owner",
            title="Scheduled task",
            prompt="test",
            next_run_at=utc_now(),
        )
        db.add(task)
        db.commit()
        run = service.start_scheduled_task_async(db, task, manual=manual)
        assert queued == [(task.id, run.id)]
        assert run.status == "running"
        assert db.get(ChatSession, run.session_id).user_id == "owner"
        # Replaying the same occurrence retains the same session and binding.
        replay = service._prepare_scheduled_task_run(db, task, run.scheduled_for, manual)
        assert replay.id == run.id
        assert replay.session_id == run.session_id
        assert len(db.exec(select(ChatSession)).all()) == 1
    engine.dispose()


@pytest.mark.parametrize("saved", [None, {**BUSINESS_BINDING, "namespace": "other-runtime"}])
def test_existing_untrusted_scheduled_session_still_rejected(monkeypatch, saved):
    monkeypatch.setattr(session_binding, "current_binding", lambda _db: BUSINESS_BINDING)
    state = {} if saved is None else {"runtime_binding": saved}
    session = ChatSession(
        id="legacy", tenant_id="tenant", user_id="owner", context_state_json=state
    )
    with pytest.raises(ModuleSdkError):
        session_binding.bind_session(object(), session)
    assert session.context_state_json == state
