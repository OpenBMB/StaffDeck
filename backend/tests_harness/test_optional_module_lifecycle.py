"""Behavior regressions for replacement adapters and disabled durable team work."""

from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.channels as channels
from app.channels.adapters import base
from app.db.models import Team, TeamTask, TeamWakeEvent, utc_now
from app.teams import wakeup
from app.teams.sweeper import sweep_timed_out_tasks
from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.modules.registry import ModuleRegistry, manifest
from staffdeck_harness.runtime.ingress import accept


class LifecycleAdapter:
    def __init__(self, *, stopped=True):
        self.calls = []
        self.stopped = stopped

    def pause_binding(self, binding_id):
        self.calls.append(("pause", binding_id))

    def wait_binding_stopped(self, binding_id, timeout_seconds):
        self.calls.append(("wait", binding_id, timeout_seconds))
        return self.stopped

    def resume_binding(self, binding_id, *, start=True):
        self.calls.append(("resume", binding_id, start))


@pytest.mark.parametrize("channel", ["feishu", "custom-channel"])
def test_replaced_adapter_owns_whole_binding_lifecycle(monkeypatch, channel):
    adapter = LifecycleAdapter()
    monkeypatch.setattr(channels, "_ensure_adapters_registered", lambda: None)
    monkeypatch.setattr(base, "get_channel_adapter", lambda name: adapter)
    monkeypatch.setattr(channels, "get_feishu_process_manager",
                        lambda: pytest.fail("replacement must not use native manager"))
    channels.pause_binding_ingress(channel, "binding")
    assert channels.wait_binding_ingress_stopped(channel, "binding", 0.1)
    channels.resume_binding_ingress(channel, "binding", start=True)
    assert adapter.calls == [("pause", "binding"), ("wait", "binding", 0.1),
                             ("resume", "binding", True)]


def test_restart_keeps_one_selected_adapter_and_honors_unfinished_drain(monkeypatch):
    adapter = LifecycleAdapter(stopped=False)
    replacements = iter([adapter, LifecycleAdapter()])
    monkeypatch.setattr(channels, "_ensure_adapters_registered", lambda: None)
    monkeypatch.setattr(base, "get_channel_adapter", lambda name: next(replacements))
    assert not channels.restart_binding_ingress("feishu", "binding", wait_seconds=0.1)
    assert adapter.calls == [("pause", "binding"), ("wait", "binding", 0.1),
                             ("resume", "binding", False)]


def test_binding_reconfiguration_pins_adapter_for_separate_api_calls(monkeypatch):
    adapter = LifecycleAdapter()
    replacements = iter([adapter, LifecycleAdapter()])
    monkeypatch.setattr(channels, "_ensure_adapters_registered", lambda: None)
    monkeypatch.setattr(base, "get_channel_adapter", lambda name: next(replacements))
    with channels.binding_lifecycle_lock("binding"):
        channels.pause_binding_ingress("feishu", "binding")
        assert channels.wait_binding_ingress_stopped("feishu", "binding", 0.1)
        channels.resume_binding_ingress("feishu", "binding", start=True)
    assert adapter.calls == [("pause", "binding"), ("wait", "binding", 0.1),
                             ("resume", "binding", True)]


def test_unknown_adapter_cannot_claim_stopped_without_confirmation(monkeypatch):
    calls = []
    adapter = SimpleNamespace(stop_ingress=lambda binding: calls.append("stop"),
                              start_ingress=lambda binding: calls.append("start"))
    monkeypatch.setattr(channels, "_ensure_adapters_registered", lambda: None)
    monkeypatch.setattr(base, "get_channel_adapter", lambda name: adapter)
    assert not channels.restart_binding_ingress("custom-channel", "binding")
    assert calls == ["stop"]


@pytest.mark.parametrize("channel,getter", [
    ("feishu", "get_feishu_process_manager"),
    ("wecom", "get_wecom_stream_manager"),
    ("wechat", "get_wechat_poll_manager"),
    ("dingtalk", "get_dingtalk_stream_manager"),
])
def test_builtin_adapters_reuse_native_manager_lifecycle(monkeypatch, channel, getter):
    channels._ensure_adapters_registered()
    manager = LifecycleAdapter()
    monkeypatch.setattr(channels, getter, lambda: manager)
    monkeypatch.setattr(base, "get_channel_adapter", base.get_builtin_channel_adapter)
    assert channels.restart_binding_ingress(channel, "binding", wait_seconds=0.1)
    assert manager.calls[-1] == ("resume", "binding", True)


def test_callback_only_adapter_explicitly_confirms_no_producer(monkeypatch):
    channels._ensure_adapters_registered()
    monkeypatch.setattr(base, "get_channel_adapter", base.get_builtin_channel_adapter)
    assert channels.restart_binding_ingress("wechat_kf", "binding")


@pytest.fixture
def team_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    registry = ModuleRegistry()
    registry.install(manifest("test.team", "Test team", kind=ModuleKind.CODE,
                              slots=[SlotName.STAFF_TEAM]),
                     object(), slot=SlotName.STAFF_TEAM, enabled=False)
    with Session(engine) as db:
        db.info["staffdeck_registry"] = registry
        team = Team(id="team", tenant_id="tenant", name="team", owner_user_id="owner")
        task = TeamTask(id="task", team_id=team.id, tenant_id="tenant", title="queued work",
                        status="pending", assignee_agent_id="worker")
        event = TeamWakeEvent(id="wake", team_id=team.id, tenant_id="tenant",
                              target_agent_id="worker", trigger_type="task_assigned",
                              payload_json={"task_id": task.id}, status="pending",
                              updated_at=utc_now() - timedelta(hours=2))
        db.add_all([team, task, event])
        db.commit()
        yield db, registry, task, event
    engine.dispose()


def test_disabled_team_does_not_enqueue_or_consume_and_resumes_same_event(monkeypatch, team_db):
    db, registry, task, event = team_db
    calls = []
    services = SimpleNamespace(registry=registry, session=lambda *a, **kw: nullcontext(db))
    monkeypatch.setattr("staffdeck_harness.runtime.services.runtime_services", lambda db: services)
    monkeypatch.setattr("app.async_jobs.enqueue_async_job", lambda name, run, **kw: run())
    monkeypatch.setattr("staffdeck_harness.runtime.actors.restore_actor", lambda *a, **kw: None)
    monkeypatch.setattr(wakeup, "_ensure_wake_target_agent", lambda *a: SimpleNamespace(id="worker"))
    monkeypatch.setattr(wakeup, "_execute_member_task", lambda *a: calls.append(event.id))

    assert not wakeup.start_wakeup_async(event.id, db=db)
    assert wakeup.dispatch_pending_wake_events(db, grace_seconds=0) == []
    wakeup._execute_wakeup_in_background(event.id, data_services=services)
    db.refresh(event)
    db.refresh(task)
    assert (event.status, task.status, calls) == ("pending", "pending", [])

    registry.set_enabled("test.team", True)
    assert wakeup.dispatch_pending_wake_events(db, grace_seconds=0) == [event.id]
    db.refresh(event)
    assert event.status == "done"
    assert calls == [event.id]
    assert wakeup.dispatch_pending_wake_events(db, grace_seconds=0) == []
    assert calls == [event.id]


def test_disabled_team_does_not_recover_or_timeout_persisted_tasks(team_db):
    db, registry, task, event = team_db
    old = utc_now() - timedelta(hours=2)
    event.status, event.updated_at = "claimed", old
    task.status, task.updated_at = "in_progress", old
    db.add_all([event, task])
    db.commit()
    assert wakeup.recover_orphaned_wake_events(db, lease_timeout_seconds=1) == []
    assert sweep_timed_out_tasks(db) == []
    db.refresh(event)
    db.refresh(task)
    assert (event.status, task.status) == ("claimed", "in_progress")
    registry.set_enabled("test.team", True)
    assert wakeup.recover_orphaned_wake_events(db, lease_timeout_seconds=1) == [event.id]
    db.refresh(event)
    assert event.status == "pending"


def test_disabled_direct_executor_returns_claimed_event_to_queue(monkeypatch, team_db):
    db, _, task, event = team_db
    event.status = "claimed"
    db.add(event)
    db.commit()
    monkeypatch.setattr(wakeup, "_ensure_wake_target_agent",
                        lambda *a: pytest.fail("disabled module must not resolve or execute staff"))
    assert wakeup.execute_wake_event(db, event).status == "pending"
    db.refresh(task)
    assert task.status == "pending"


@pytest.mark.parametrize("channel,mode", [("team", "team_task"), ("web", "team_tl")])
def test_team_ingress_rejects_disabled_module_before_agentloop(team_db, channel, mode):
    _, registry, _, _ = team_db
    request = SimpleNamespace(channel=channel, interaction_mode=mode)
    with pytest.raises(ModuleSdkError) as error:
        accept(registry, request)
    assert error.value.code == "TEAM_MODULE_DISABLED"
