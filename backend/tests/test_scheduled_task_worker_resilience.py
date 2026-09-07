"""A transient sweep failure (e.g. SQLite "database is locked") must not end the worker thread."""

from __future__ import annotations

import logging
from contextlib import nullcontext

from sqlalchemy.exc import OperationalError

from app.scheduled_tasks import worker


def test_worker_logs_and_continues_when_a_sweep_fails(monkeypatch, caplog) -> None:
    calls = []

    def flaky_due(db):
        calls.append(1)
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(worker, "init_db", lambda: None)
    monkeypatch.setattr(worker, "seed_demo_data", lambda db: None)
    monkeypatch.setattr(worker, "Session", lambda engine: nullcontext(object()))
    monkeypatch.setattr(worker, "due_scheduled_tasks", flaky_due)
    monkeypatch.setattr(worker, "_stopped", False)
    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        worker.run_worker(once=True, poll_seconds=1.0)  # must not raise
    assert calls == [1]
    assert any("scheduled-task sweep failed" in r.getMessage() for r in caplog.records), "the failure is visible in the log"
