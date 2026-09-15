from types import SimpleNamespace as NS
import pytest
from staffdeck_harness.modules.registry import ModuleRegistry


def test_background_job_keeps_generation_pinned_while_queued_and_running(monkeypatch):
    from app import async_jobs
    from staffdeck_harness.runtime.jobs import enqueue_runtime_job
    queued = []
    monkeypatch.setattr(async_jobs, "enqueue_async_job", lambda name, func, **kw: queued.append(func))
    reg = ModuleRegistry()
    completed = []
    enqueue_runtime_job("test", lambda: completed.append(True), registry=reg)
    assert reg.live_turns == 1 and not completed
    reg.begin_drain()
    queued.pop()()
    assert reg.live_turns == 0 and completed == [True]


def test_rejected_queue_submission_releases_generation(monkeypatch):
    from app import async_jobs
    from staffdeck_harness.runtime.jobs import enqueue_runtime_job
    def rejected(*a, **kw):
        raise RuntimeError("stopping")
    monkeypatch.setattr(async_jobs, "enqueue_async_job", rejected)
    reg = ModuleRegistry()
    with pytest.raises(RuntimeError):
        enqueue_runtime_job("test", lambda: None, registry=reg)
    assert reg.live_turns == 0


def test_memory_worker_uses_pinned_session_and_model_not_local_engine(monkeypatch):
    from contextlib import contextmanager
    from app.memory import jobs
    from app.session.session_schema import ChatTurnRequest, StepAgentResult
    calls = []
    @contextmanager
    def session(identity, *, purpose):
        calls.append(purpose)
        yield NS(get=lambda *a: None, commit=lambda: None)
    monkeypatch.setattr(jobs, "Session", lambda *a: pytest.fail("local database fallback"))
    monkeypatch.setattr(jobs, "EventLog", lambda db: NS(record=lambda *a: None))
    jobs.run_memory_capture_job({"request":ChatTurnRequest(tenant_id="t",message="test").model_dump(),
        "session_id":"s", "model_config_id":"remote-model", "step_result":StepAgentResult(action="reply",reply="test").model_dump(),
        "tool_result":None}, data_services=NS(session=session), model_config=NS(id="remote-model"))
    assert calls == ["memory.capture"]
