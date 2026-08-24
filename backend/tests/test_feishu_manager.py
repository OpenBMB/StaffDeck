from __future__ import annotations

import time

from sqlmodel import Session, SQLModel, create_engine

from app.channels.feishu_manager import FeishuProcessManager
from app.channels.feishu_process import ConnectorState
from app.db.models import ChannelBinding, Tenant


class FakeSupervisor:
    def __init__(self, **kwargs):
        self.database_url = kwargs["database_url"]
        self.started = []
        self.stopped = []
        self.states = {}
        self.connections = {}
        self.closed = False
        self.allow_stop = True

    def start_binding(self, binding_id, revision):
        self.started.append((binding_id, revision))
        was_running = self.states.get(binding_id) is ConnectorState.RUNNING
        self.states[binding_id] = ConnectorState.RUNNING
        if not was_running:
            self.connections[binding_id] = True

    def stop_binding(self, binding_id, timeout=5.0):
        self.stopped.append((binding_id, timeout))
        if not self.allow_stop:
            return False
        self.states[binding_id] = ConnectorState.ABSENT
        self.connections[binding_id] = False
        return True

    def state(self, binding_id):
        return self.states.get(binding_id, ConnectorState.ABSENT)

    def connected(self, binding_id):
        return self.connections.get(binding_id, False)

    def stop(self, timeout=5.0):
        self.closed = True
        return True

    def reset_backoff(self, binding_id):
        self.states.pop(binding_id, None)


def test_manager_reconciles_pause_resume_revision_and_shutdown(tmp_path) -> None:
    db_path = tmp_path / "manager.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="A"))
        db.add(
            ChannelBinding(
                id="chan_feishu",
                tenant_id="tenant_a",
                agent_id="agent_a",
                channel="feishu",
                status="active",
                config_revision=4,
            )
        )
        db.commit()
    created = []

    def factory(**kwargs):
        supervisor = FakeSupervisor(**kwargs)
        created.append(supervisor)
        return supervisor

    manager = FeishuProcessManager(
        db_engine=engine,
        supervisor_factory=factory,
        reconcile_seconds=60,
    )
    manager.reconcile_once()
    supervisor = created[0]
    assert supervisor.started == [("chan_feishu", 4)]

    manager.pause_binding("chan_feishu")
    assert manager.wait_binding_stopped("chan_feishu", timeout_seconds=1.0)
    assert supervisor.stopped[-1][0] == "chan_feishu"
    manager.resume_binding("chan_feishu")
    assert supervisor.started[-1] == ("chan_feishu", 4)

    with Session(engine) as db:
        binding = db.get(ChannelBinding, "chan_feishu")
        binding.config_revision = 5
        db.add(binding)
        db.commit()
    manager.reconcile_once()
    assert supervisor.stopped[-1][0] == "chan_feishu"
    assert supervisor.started[-1] == ("chan_feishu", 5)
    assert manager.stop(timeout_seconds=1.0) is True
    assert supervisor.closed is True


def test_manager_keeps_tracking_binding_when_stop_times_out(tmp_path) -> None:
    db_path = tmp_path / "manager-stop-timeout.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="A"))
        db.add(
            ChannelBinding(
                id="chan_feishu",
                tenant_id="tenant_a",
                agent_id="agent_a",
                channel="feishu",
                status="active",
                config_revision=1,
            )
        )
        db.commit()
    created = []

    def factory(**kwargs):
        supervisor = FakeSupervisor(**kwargs)
        created.append(supervisor)
        return supervisor

    manager = FeishuProcessManager(db_engine=engine, supervisor_factory=factory)
    manager.reconcile_once()
    supervisor = created[0]
    supervisor.allow_stop = False
    with Session(engine) as db:
        binding = db.get(ChannelBinding, "chan_feishu")
        binding.status = "disabled"
        db.add(binding)
        db.commit()

    manager.reconcile_once()
    manager.reconcile_once()
    assert len(supervisor.stopped) == 2
    supervisor.allow_stop = True
    assert manager.stop(timeout_seconds=1.0) is True


def test_manager_writes_disconnected_truth_and_accepts_backoff_as_stopped(tmp_path) -> None:
    db_path = tmp_path / "manager-disconnect.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="A"))
        db.add(
            ChannelBinding(
                id="chan_feishu",
                tenant_id="tenant_a",
                agent_id="agent_a",
                channel="feishu",
                status="active",
                config_revision=1,
            )
        )
        db.commit()
    created = []

    def factory(**kwargs):
        supervisor = FakeSupervisor(**kwargs)
        created.append(supervisor)
        return supervisor

    manager = FeishuProcessManager(db_engine=engine, supervisor_factory=factory)
    manager.reconcile_once()
    supervisor = created[0]
    manager.reconcile_once()
    with Session(engine) as db:
        assert db.get(ChannelBinding, "chan_feishu").connected is True
    supervisor.connections["chan_feishu"] = False
    manager.reconcile_once()
    with Session(engine) as db:
        assert db.get(ChannelBinding, "chan_feishu").connected is False
    supervisor.states["chan_feishu"] = ConnectorState.CRASH_BACKOFF
    assert manager.wait_binding_stopped("chan_feishu", timeout_seconds=0.01) is True
    assert manager.stop(timeout_seconds=1.0) is True


def test_pause_starts_stop_without_consuming_wait_deadline(tmp_path) -> None:
    db_path = tmp_path / "manager-async-stop.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="A"))
        db.add(
            ChannelBinding(
                id="chan_feishu",
                tenant_id="tenant_a",
                agent_id="agent_a",
                channel="feishu",
                status="active",
                config_revision=1,
            )
        )
        db.commit()

    class SlowSupervisor(FakeSupervisor):
        def stop_binding(self, binding_id, timeout=5.0):
            time.sleep(0.15)
            return super().stop_binding(binding_id, timeout)

    manager = FeishuProcessManager(
        db_engine=engine,
        supervisor_factory=lambda **kwargs: SlowSupervisor(**kwargs),
    )
    manager.reconcile_once()
    started = time.monotonic()
    manager.pause_binding("chan_feishu")
    assert time.monotonic() - started < 0.05
    assert manager.wait_binding_stopped("chan_feishu", timeout_seconds=1.0)
    assert manager.stop(timeout_seconds=1.0)


def test_connected_writeback_is_revision_fenced_in_single_update(tmp_path) -> None:
    db_path = tmp_path / "manager-connected-cas.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="A"))
        db.add(
            ChannelBinding(
                id="chan_feishu",
                tenant_id="tenant_a",
                agent_id="agent_a",
                channel="feishu",
                status="active",
                connected=False,
                config_revision=1,
            )
        )
        db.commit()

    class RevisionRacingSupervisor(FakeSupervisor):
        raced = False

        def connected(self, binding_id):
            if not self.raced:
                self.raced = True
                with Session(engine) as db:
                    binding = db.get(ChannelBinding, binding_id)
                    binding.config_revision = 2
                    db.add(binding)
                    db.commit()
            return True

    manager = FeishuProcessManager(
        db_engine=engine,
        supervisor_factory=lambda **kwargs: RevisionRacingSupervisor(**kwargs),
    )
    manager.reconcile_once()
    with Session(engine) as db:
        binding = db.get(ChannelBinding, "chan_feishu")
        assert binding.config_revision == 2
        assert binding.connected is False
    assert manager.stop(timeout_seconds=1.0)


def test_channel_service_lifecycle_starts_and_stops_feishu_manager(monkeypatch) -> None:
    import app.channels as channels
    import app.channels.service_intake as intake
    import app.channels.service_outbox as outbox

    class Manager:
        def __init__(self):
            self.started = 0
            self.stopped = 0

        def start(self):
            self.started += 1

        def stop(self, timeout_seconds=5.0):
            self.stopped += 1
            return True

    wechat = Manager()
    wecom = Manager()
    feishu = Manager()
    monkeypatch.setattr(channels, "_wechat_poll_manager", wechat)
    monkeypatch.setattr(channels, "_wecom_stream_manager", wecom)
    monkeypatch.setattr(channels, "_feishu_process_manager", feishu)
    monkeypatch.setattr(channels, "_acquire_connector_process_lock", lambda: True)
    monkeypatch.setattr(channels, "_release_connector_process_lock", lambda: None)
    monkeypatch.setattr(channels, "_ensure_adapters_registered", lambda: None)
    monkeypatch.setattr(intake, "start_staged_inbound_daemon", lambda: None)
    monkeypatch.setattr(intake, "stop_staged_inbound_daemon", lambda timeout_seconds=5.0: True)
    monkeypatch.setattr(intake, "sweep_stale_inbound_events", lambda: 0)
    monkeypatch.setattr(outbox, "start_delivery_daemon", lambda: None)
    monkeypatch.setattr(outbox, "stop_delivery_daemon", lambda timeout_seconds=5.0: True)

    channels.start_channel_services()
    assert (wechat.started, wecom.started, feishu.started) == (1, 1, 1)
    assert channels.stop_channel_services(timeout_seconds=1.0) is True
    assert (wechat.stopped, wecom.stopped, feishu.stopped) == (1, 1, 1)


def test_feishu_adapter_is_registered() -> None:
    import app.channels as channels
    from app.channels.adapters.base import get_channel_adapter

    channels._ensure_adapters_registered()
    assert get_channel_adapter("feishu").__class__.__name__ == "FeishuAdapter"


# ---------- 数据库 URL 透传(P2 方言化) ----------


def test_manager_accepts_non_sqlite_database_url() -> None:
    """非 SQLite 不再报"仅支持文件 SQLite":完整 URL(密码不遮蔽)透传给子进程。"""
    engine = create_engine("postgresql+psycopg://user:secret@db.internal:5432/staffdeck")
    manager = FeishuProcessManager(db_engine=engine, reconcile_seconds=60)
    assert (
        manager._database_url
        == "postgresql+psycopg://user:secret@db.internal:5432/staffdeck"
    )


def test_manager_rejects_memory_sqlite() -> None:
    """内存 SQLite 子进程无法共享,仍然拒绝(与方言无关的物理限制)。"""
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", poolclass=StaticPool)
    try:
        FeishuProcessManager(db_engine=engine)
    except RuntimeError as error:
        assert "内存数据库" in str(error)
    else:
        raise AssertionError("内存 SQLite 必须拒绝(子进程无法共享)")
