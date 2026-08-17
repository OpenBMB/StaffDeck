from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from time import sleep
from types import SimpleNamespace

import app.async_jobs as async_jobs
from app.async_jobs import AsyncJobQueue
from app.core.agent_loop import AgentLoop
from app.db.models import ChatSession, ModelConfig
from app.session.session_schema import ChatTurnRequest, StepAgentResult


def test_async_job_queue_runs_job_without_calling_inline() -> None:
    queue = AsyncJobQueue(max_workers=1)
    started = Event()
    release = Event()

    def job_func() -> None:
        started.set()
        release.wait(1)

    try:
        job = queue.enqueue("test.job", job_func)

        assert job.status in {"queued", "running"}
        assert started.wait(1)
        release.set()
        assert _eventually_succeeded(queue, job.id)
    finally:
        release.set()
        queue.shutdown()


def test_async_job_queue_rejects_enqueue_after_shutdown_without_phantom_job() -> None:
    queue = AsyncJobQueue(max_workers=1)
    queue.shutdown()

    try:
        queue.enqueue("test.rejected", lambda: None)
    except RuntimeError as exc:
        assert "no longer accepts jobs" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("enqueue after shutdown was accepted")

    assert queue.list_recent() == []


def test_async_job_queue_shutdown_terminalizes_all_accepted_jobs() -> None:
    queue = AsyncJobQueue(max_workers=1)
    started = Event()
    release = Event()

    def blocking_job() -> None:
        started.set()
        release.wait(2)

    running = queue.enqueue("test.running", blocking_job)
    queued = queue.enqueue("test.queued", lambda: None)
    assert started.wait(1)

    shutdown = Thread(target=queue.shutdown)
    shutdown.start()
    assert shutdown.is_alive()
    release.set()
    shutdown.join(2)

    assert not shutdown.is_alive()
    assert queue.get(running.id).status == "succeeded"  # type: ignore[union-attr]
    assert queue.get(queued.id).status in {"succeeded", "cancelled"}  # type: ignore[union-attr]
    assert all(job.status in {"succeeded", "failed", "cancelled"} for job in queue.list_recent())


def test_enqueue_racing_with_shutdown_leaves_no_phantom_job() -> None:
    queue = AsyncJobQueue(max_workers=1)
    queue._executor.shutdown(wait=True)  # noqa: SLF001 - controlled race fixture.
    submit_entered = Event()
    release_submit = Event()
    delegate = ThreadPoolExecutor(max_workers=1)

    class GatedExecutor:
        def submit(self, *args, **kwargs):  # noqa: ANN002, ANN003
            submit_entered.set()
            release_submit.wait(2)
            return delegate.submit(*args, **kwargs)

        def shutdown(self, *, wait=True, cancel_futures=False):  # noqa: ANN001
            delegate.shutdown(wait=wait, cancel_futures=cancel_futures)

    queue._executor = GatedExecutor()  # type: ignore[assignment]  # noqa: SLF001
    errors: list[Exception] = []

    def enqueue() -> None:
        try:
            queue.enqueue("test.race", lambda: None)
        except Exception as exc:  # noqa: BLE001 - capture the expected rejection.
            errors.append(exc)

    enqueue_thread = Thread(target=enqueue)
    enqueue_thread.start()
    assert submit_entered.wait(1)

    shutdown_thread = Thread(target=queue.shutdown)
    shutdown_thread.start()
    shutdown_thread.join(1)
    assert not shutdown_thread.is_alive()

    release_submit.set()
    enqueue_thread.join(2)
    assert not enqueue_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert queue.list_recent() == []


def test_start_async_jobs_replaces_queue_closed_by_prior_app_lifecycle(monkeypatch) -> None:
    closed_queue = AsyncJobQueue(max_workers=1)
    closed_queue.shutdown()
    monkeypatch.setattr(async_jobs, "_default_queue", closed_queue)

    restarted_queue = async_jobs.start_async_jobs()

    try:
        assert restarted_queue is not closed_queue
        assert restarted_queue.accepting is True
        job = restarted_queue.enqueue("test.after-restart", lambda: None)
        assert _eventually_succeeded(restarted_queue, job.id)
    finally:
        restarted_queue.shutdown()


def test_agent_loop_enqueues_memory_capture_without_running_it_inline(monkeypatch) -> None:
    captured = {}

    def fake_enqueue_memory_capture(*args):  # noqa: ANN002
        captured["args"] = args
        return SimpleNamespace(id="job_memory_1", name="memory.capture_turn")

    monkeypatch.setattr("app.core.agent_loop.enqueue_memory_capture", fake_enqueue_memory_capture)

    loop = object.__new__(AgentLoop)
    loop.events = _FakeEvents()
    loop.db = _FakeDb()

    result = loop._enqueue_memory_capture(
        ChatTurnRequest(tenant_id="tenant_demo", user_id="user_demo", message="我叫hm"),
        ChatSession(id="session_test", tenant_id="tenant_demo", user_id="user_demo"),
        StepAgentResult(),
        None,
        ModelConfig(
            id="model_test",
            tenant_id="tenant_demo",
            name="demo",
            api_key_encrypted="encrypted",
            model="demo",
        ),
    )

    assert result == [{"job_id": "job_memory_1", "job_name": "memory.capture_turn"}]
    assert captured["args"][1] == "session_test"
    assert captured["args"][4] == "model_test"
    assert loop.events.records[0][2] == "async_job_enqueued"
    assert loop.db.commits == 1


def _eventually_succeeded(queue: AsyncJobQueue, job_id: str) -> bool:
    for _ in range(20):
        job = queue.get(job_id)
        if job and job.status == "succeeded":
            return True
        sleep(0.01)
    return False


class _FakeEvents:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict]] = []

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict) -> None:
        self.records.append((tenant_id, session_id, event_type, payload))


class _FakeDb:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1
