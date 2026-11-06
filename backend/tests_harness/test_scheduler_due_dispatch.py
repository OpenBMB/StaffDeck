from datetime import timedelta
from types import SimpleNamespace

from app.db.models import ScheduledTask, utc_now
from app.scheduled_tasks import worker
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


def assembly(monkeypatch, db, dispatch, enabled=True):
    from staffdeck_harness.modules import registry
    from staffdeck_harness.runtime import services

    providers = (
        [
            SimpleNamespace(
                manifest=SimpleNamespace(module_id="ingress.scheduler", metadata={}),
                provider=SimpleNamespace(dispatch=dispatch),
            )
        ]
        if enabled
        else []
    )
    monkeypatch.setattr(
        registry, "peek_registry", lambda: SimpleNamespace(providers=lambda _slot: providers)
    )
    monkeypatch.setattr(services, "maintenance_sessions", lambda: iter([db]))
    monkeypatch.setattr(worker, "_scheduler_ready", None)


def test_enabled_scheduler_claims_only_active_due_tasks_once(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[ScheduledTask.__table__])
    now = utc_now()
    calls = []
    with Session(engine) as db:
        for identifier, status, next_run in (
            ("due", "active", now - timedelta(seconds=1)),
            ("future", "active", now + timedelta(days=1)),
            ("paused", "paused", now - timedelta(seconds=1)),
            ("completed", "completed", now - timedelta(seconds=1)),
            ("archived", "archived", now - timedelta(seconds=1)),
        ):
            db.add(
                ScheduledTask(
                    id=identifier,
                    tenant_id="tenant",
                    agent_id="staff",
                    created_by_user_id="owner",
                    title=identifier,
                    prompt="test",
                    status=status,
                    next_run_at=next_run,
                )
            )
        db.commit()
        assembly(monkeypatch, db, lambda _db, task: calls.append(task.id))
        worker._run_due_tasks()
        worker._run_due_tasks()
        assert calls == ["due"]
        assert db.get(ScheduledTask, "due").lease_until > now
        assert db.get(ScheduledTask, "future").lease_owner is None
    engine.dispose()


def test_explicitly_disabled_scheduler_never_claims_tasks(monkeypatch):
    assembly(monkeypatch, object(), lambda *_args: None, enabled=False)
    monkeypatch.setattr(
        worker,
        "due_scheduled_tasks",
        lambda _db: (_ for _ in ()).throw(AssertionError("disabled scheduler must not claim")),
    )
    worker._run_due_tasks()


def test_one_failed_admission_does_not_block_other_due_tasks(monkeypatch):
    calls, rollbacks = [], []

    def dispatch(_db, task):
        calls.append(task.id)
        if task.id == "broken":
            raise RuntimeError("transient directory failure")

    db = SimpleNamespace(rollback=lambda: rollbacks.append(True))
    assembly(monkeypatch, db, dispatch)
    monkeypatch.setattr(
        worker,
        "due_scheduled_tasks",
        lambda _db: [SimpleNamespace(id="broken"), SimpleNamespace(id="healthy")],
    )
    worker._run_due_tasks()
    assert calls == ["broken", "healthy"]
    assert rollbacks == [True]
