"""Worker admission is bounded; tracker state and network calls remain shared."""
from concurrent.futures import Future
from contextlib import contextmanager
import threading
from types import SimpleNamespace

import pytest

from app.tools import external_task_dispatch as dispatch_module
from app.tools import external_tasks
from staffdeck_harness.modules import registry as registry_module
from staffdeck_harness.modules.registry import ModuleRegistry


class Session:
    def __init__(self, tasks, *, services=None, registry=None):
        self.tasks = tasks
        self.info = {"staffdeck_registry": registry}
        self.services = services
        self.created_on = threading.get_ident()
        self.closed = False
        self.rollbacks = 0

    def get(self, model, task_id):
        assert threading.get_ident() == self.created_on, "Session was shared across worker threads"
        return self.tasks.get(task_id)

    def rollback(self):
        self.rollbacks += 1


class Services:
    def __init__(self, tasks, registry, namespace="test-runtime"):
        self.tasks, self.registry, self.namespace = tasks, registry, namespace
        self.sessions = []
        self.identities = []
        self.purposes = []

    @contextmanager
    def session(self, identity, *, purpose):
        db = Session(self.tasks, registry=self.registry)
        self.sessions.append(db)
        self.identities.append(identity)
        self.purposes.append(purpose)
        try:
            yield db
        finally:
            db.closed = True


@pytest.fixture
def context(monkeypatch):
    registry = ModuleRegistry()
    monkeypatch.setattr(registry_module, "_active", registry)
    tasks = {name: SimpleNamespace(id=name, tenant_id="tenant")
             for name in ("slow", "fast", "another", "poll")}
    services = Services(tasks, registry)
    scan = Session(tasks, services=services, registry=registry)
    monkeypatch.setattr(dispatch_module, "runtime_services", lambda db, reg: db.services)
    return registry, services, scan


def test_slow_submit_does_not_block_other_submit_or_poll(context, monkeypatch):
    registry, services, scan = context
    started = {name: threading.Event() for name in ("slow", "fast", "poll")}
    release = threading.Event()
    calls = []

    def execute(db, task_id, phase):
        assert db is not scan
        assert db.created_on == threading.get_ident()
        assert db.info["staffdeck_registry"] is registry
        calls.append((task_id, phase))
        started[task_id].set()
        assert release.wait(3)

    monkeypatch.setattr(external_tasks, "execute_external_task", execute)
    pool = dispatch_module.ExternalTaskDispatcher(submit_workers=2, poll_workers=1)
    try:
        assert pool.dispatch(scan, "slow", "submit")
        assert started["slow"].wait(2)
        assert pool.dispatch(scan, "fast", "submit")
        assert started["fast"].wait(2)
        assert not pool.dispatch(scan, "another", "submit")  # Remains in the DB.
        assert pool.dispatch(scan, "poll", "poll")
        assert started["poll"].wait(2)
        assert registry.live_turns == 3
        for _ in range(4):
            assert not pool.dispatch(scan, "slow", "submit")
            assert not pool.dispatch(scan, "slow", "poll")
    finally:
        release.set()
        pool.close()
    assert sorted(calls) == [("fast", "submit"), ("poll", "poll"), ("slow", "submit")]
    assert len({id(db) for db in services.sessions}) == 3
    assert all(db.closed for db in services.sessions)
    assert {identity.tenant_id for identity in services.identities} == {"tenant"}
    assert set(services.purposes) == {"external-task-submit", "external-task-poll"}
    assert registry.live_turns == 0


def test_drain_rejects_new_work_but_counts_already_started_call(context, monkeypatch):
    registry, _, scan = context
    started, release = threading.Event(), threading.Event()

    def execute(*args):
        started.set()
        assert release.wait(3)

    monkeypatch.setattr(external_tasks, "execute_external_task", execute)
    pool = dispatch_module.ExternalTaskDispatcher(submit_workers=2, poll_workers=1)
    try:
        assert pool.dispatch(scan, "slow", "submit")
        assert started.wait(2)
        registry.begin_drain()
        assert not pool.dispatch(scan, "fast", "submit")
        assert not pool.dispatch(scan, "poll", "poll")
        assert registry.live_turns == 1
    finally:
        release.set()
        pool.close()
    assert registry.live_turns == 0


def test_stop_waits_for_started_network_call_without_resubmitting(context, monkeypatch):
    registry, _, scan = context
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def execute(db, task_id, phase):
        calls.append(task_id)
        started.set()
        assert release.wait(3)

    monkeypatch.setattr(external_tasks, "execute_external_task", execute)
    pool = dispatch_module.ExternalTaskDispatcher()
    assert pool.dispatch(scan, "slow", "submit")
    assert started.wait(2)
    def stop():
        pool.close()
        closed.set()
    thread = threading.Thread(target=stop)
    thread.start()
    try:
        assert not closed.wait(0.05)
        assert registry.live_turns == 1
        assert not pool.dispatch(scan, "fast", "submit")
    finally:
        release.set()
        thread.join(timeout=2)
    assert closed.is_set()
    assert calls == ["slow"]
    assert registry.live_turns == 0


class DeferredExecutor:
    def __init__(self, **kwargs):
        self.jobs = []

    def submit(self, function, *args):
        self.jobs.append((function, args))
        return Future()

    def run(self):
        while self.jobs:
            function, args = self.jobs.pop(0)
            function(*args)

    def shutdown(self, **kwargs):
        self.run()


@pytest.mark.parametrize("transition", ["generation", "registry", "stop", "tenant"])
def test_stale_or_stopped_offer_never_claims_task(context, monkeypatch, transition):
    registry, services, scan = context
    monkeypatch.setattr(dispatch_module, "ThreadPoolExecutor", DeferredExecutor)
    calls = []
    monkeypatch.setattr(external_tasks, "execute_external_task", lambda *args: calls.append(args))
    pool = dispatch_module.ExternalTaskDispatcher()
    assert pool.dispatch(scan, "slow", "submit")
    assert registry.live_turns == 1
    if transition == "generation":
        registry.generation += 1
    elif transition == "registry":
        monkeypatch.setattr(registry_module, "_active", ModuleRegistry())
    elif transition == "tenant":
        services.tasks["slow"] = SimpleNamespace(id="slow", tenant_id="other")
    if transition != "stop":
        pool._executors["submit"].run()
    pool.close()
    assert calls == []
    assert registry.live_turns == 0


def test_deduplication_is_namespaced_and_tenant_scoped(context, monkeypatch):
    registry, services, scan = context
    monkeypatch.setattr(dispatch_module, "ThreadPoolExecutor", DeferredExecutor)
    calls = []
    monkeypatch.setattr(external_tasks, "execute_external_task", lambda *args: calls.append(args))
    pool = dispatch_module.ExternalTaskDispatcher(submit_workers=3)
    other_namespace = Services(scan.tasks, registry, namespace="another-runtime")
    other_tenant = Services({"slow": SimpleNamespace(id="slow", tenant_id="another")}, registry)
    assert pool.dispatch(scan, "slow", "submit")
    assert not pool.dispatch(scan, "slow", "poll")
    assert pool.dispatch(Session(scan.tasks, services=other_namespace, registry=registry), "slow", "submit")
    assert pool.dispatch(Session(other_tenant.tasks, services=other_tenant, registry=registry), "slow", "submit")
    pool._executors["submit"].run()
    pool.close()
    assert len(calls) == 3
    assert registry.live_turns == 0


def test_execution_exception_rolls_back_and_releases_capacity_without_replay(context, monkeypatch):
    registry, services, scan = context
    monkeypatch.setattr(dispatch_module, "ThreadPoolExecutor", DeferredExecutor)
    calls = []

    def execute(db, task_id, phase):
        calls.append(task_id)
        if task_id == "slow":
            raise RuntimeError("injected after tracker claim")

    monkeypatch.setattr(external_tasks, "execute_external_task", execute)
    pool = dispatch_module.ExternalTaskDispatcher(submit_workers=1)
    assert pool.dispatch(scan, "slow", "submit")
    pool._executors["submit"].run()
    assert registry.live_turns == 0
    assert pool.dispatch(scan, "fast", "submit")
    pool._executors["submit"].run()
    pool.close()
    assert calls == ["slow", "fast"]
    assert [db.rollbacks for db in services.sessions] == [1, 0]
    assert all(db.closed for db in services.sessions)


def test_real_tracker_scan_claims_in_independent_sqlite_sessions(tmp_path, monkeypatch):
    """Actual scan/claim transitions with fake network I/O, not a replacement tracker."""
    from datetime import timedelta
    from sqlmodel import Session as SQLSession, create_engine
    from app.db.models import ExternalBusinessTask, utc_now
    from staffdeck_harness.runtime import external_tasks as runtime_external_tasks

    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
        connect_args={"check_same_thread": False})
    ExternalBusinessTask.__table__.create(engine)
    registry = ModuleRegistry()
    monkeypatch.setattr(registry_module, "_active", registry)
    started = {name: threading.Event() for name in ("slow", "fast", "poll")}
    release = threading.Event()
    calls, used_sessions = [], []

    @contextmanager
    def resolve(db, task):
        yield SimpleNamespace()

    def submit(db, task, tool):
        calls.append((task.id, "submit"))
        used_sessions.append(db)
        started[task.id].set()
        if task.id == "slow":
            assert release.wait(3)
        task.status = "accepted"
        task.external_task_id = f"provider-{task.id}"
        db.add(task)
        db.commit()

    def poll(db, task, *, owner):
        calls.append((task.id, "poll"))
        used_sessions.append(db)
        assert task.lease_owner == owner
        task.status = "completed"
        task.lease_owner = None
        task.lease_expires_at = None
        db.add(task)
        db.commit()
        started[task.id].set()

    monkeypatch.setattr(runtime_external_tasks, "resolve_task_tool", resolve)
    monkeypatch.setattr(external_tasks, "_submit_provider_task", submit)
    monkeypatch.setattr(external_tasks, "_poll_task", poll)
    with SQLSession(engine) as db:
        for name in ("slow", "fast", "poll"):
            db.add(ExternalBusinessTask(id=name, tenant_id="tenant", user_id="user", tool_id=name,
                callback_token_hash="test", status="accepted" if name == "poll" else "queued",
                status_config_json={"async_strategy": "provider_task"},
                external_task_id="provider-poll" if name == "poll" else None,
                status_url="https://unused.invalid/status" if name == "poll" else None,
                next_poll_at=utc_now() - timedelta(seconds=1) if name == "poll" else None))
        db.commit()
    pool = dispatch_module.ExternalTaskDispatcher(submit_workers=2, poll_workers=1)
    try:
        with SQLSession(engine) as scan:
            scan.info["staffdeck_registry"] = registry
            assert external_tasks.poll_due_external_tasks(scan, dispatch=pool.dispatch) == 3
        assert all(event.wait(2) for event in started.values())
        with SQLSession(engine) as scan:
            scan.info["staffdeck_registry"] = registry
            external_tasks.poll_due_external_tasks(scan, dispatch=pool.dispatch)
        assert len({id(session) for session in used_sessions}) == 3
        assert sorted(calls) == [("fast", "submit"), ("poll", "poll"), ("slow", "submit")]
    finally:
        release.set()
        pool.close()
    with SQLSession(engine) as db:
        assert db.get(ExternalBusinessTask, "slow").status == "accepted"
        assert db.get(ExternalBusinessTask, "fast").status == "accepted"
        assert db.get(ExternalBusinessTask, "poll").status == "completed"
        assert all(db.get(ExternalBusinessTask, name).lease_owner is None for name in started)
    assert registry.live_turns == 0
    engine.dispose()
