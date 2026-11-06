from types import SimpleNamespace

import pytest

from app.tools import external_task_worker as worker


@pytest.mark.parametrize("failure", ["settings", "session", "poll"])
def test_worker_recovers_on_next_iteration_without_restart(monkeypatch, failure):
    class Stop:
        passes = 0

        def is_set(self):
            return self.passes >= 2

        def wait(self, interval):
            assert interval >= 0.5
            self.passes += 1

    stop = Stop()
    processed, rolled_back = [], []
    db = SimpleNamespace(rollback=lambda: rolled_back.append(True))

    def settings():
        if stop.passes == 0 and failure == "settings":
            raise ValueError("injected settings failure")
        return SimpleNamespace(external_task_poll_seconds=0.5)

    def sessions():
        if stop.passes == 0 and failure == "session":
            raise RuntimeError("injected session failure")
        yield db

    def poll(session, *, dispatch):
        assert session is db
        assert callable(dispatch)
        if stop.passes == 0 and failure == "poll":
            raise RuntimeError("injected poll failure")
        processed.append(True)

    monkeypatch.setattr(worker, "_stop_event", stop)
    monkeypatch.setattr(worker, "get_settings", settings)
    monkeypatch.setattr(worker, "poll_due_external_tasks", poll)
    monkeypatch.setattr("staffdeck_harness.runtime.services.maintenance_sessions", sessions)
    worker.run_external_task_worker()
    assert processed == [True]
    assert bool(rolled_back) == (failure == "poll")
