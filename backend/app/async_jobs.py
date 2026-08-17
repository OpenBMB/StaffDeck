from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any

from app.db.models import new_id, utc_now


AsyncJobStatus = str


@dataclass
class AsyncJob:
    id: str
    name: str
    status: AsyncJobStatus = "queued"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class AsyncJobQueue:
    def __init__(self, max_workers: int = 4, max_history: int = 500):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ultrarag-job")
        self._lock = Lock()
        self._jobs: dict[str, AsyncJob] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._max_history = max_history
        self._accepting = True

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    def enqueue(
        self,
        name: str,
        func: Callable[..., Any],
        *args: Any,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncJob:
        job = AsyncJob(id=new_id("job"), name=name, metadata=metadata or {})
        with self._lock:
            if not self._accepting:
                raise RuntimeError("AsyncJobQueue is shutting down and no longer accepts jobs")
            self._jobs[job.id] = job
            self._trim_history_locked()
        try:
            future = self._executor.submit(self._run_job, job.id, func, args, kwargs)
        except Exception:
            # A rejected submission was never accepted. Do not expose a
            # phantom queued handle or let it consume history capacity.
            with self._lock:
                self._jobs.pop(job.id, None)
            raise
        with self._lock:
            self._futures[job.id] = future
        future.add_done_callback(lambda completed, job_id=job.id: self._finish_future(job_id, completed))
        return job

    def get(self, job_id: str) -> AsyncJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self, limit: int = 100) -> list[AsyncJob]:
        with self._lock:
            rows = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
        return rows[:limit]

    def shutdown(self) -> None:
        with self._lock:
            self._accepting = False
        # An accepted in-memory job must reach a terminal state before shutdown
        # returns. Pending work is cancelled; already-running work is drained.
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            now = utc_now()
            for job in self._jobs.values():
                if job.status == "queued":
                    job.status = "cancelled"
                    job.finished_at = now
                    job.error = "Job cancelled because the service is shutting down"
            self._futures.clear()
            self._trim_history_locked()

    def _finish_future(self, job_id: str, future: Future[Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and future.cancelled() and job.status == "queued":
                job.status = "cancelled"
                job.finished_at = utc_now()
                job.error = "Job cancelled because the service is shutting down"
            self._futures.pop(job_id, None)

    def _run_job(
        self,
        job_id: str,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self._update(job_id, status="running", started_at=utc_now())
        try:
            func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - background jobs must never crash the request path.
            self._update(job_id, status="failed", finished_at=utc_now(), error=str(exc))
            return
        self._update(job_id, status="succeeded", finished_at=utc_now(), error=None)

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for key, value in changes.items():
                setattr(job, key, value)

    def _trim_history_locked(self) -> None:
        overflow = len(self._jobs) - self._max_history
        if overflow <= 0:
            return
        removable = sorted(
            (
                job
                for job in self._jobs.values()
                if job.status in {"succeeded", "failed", "cancelled"}
            ),
            key=lambda item: item.created_at,
        )
        for job in removable[:overflow]:
            self._jobs.pop(job.id, None)


_default_queue_lock = Lock()
_default_queue = AsyncJobQueue()


def start_async_jobs() -> AsyncJobQueue:
    """Ensure a fresh process-local executor exists for this app lifecycle."""
    global _default_queue
    with _default_queue_lock:
        if not _default_queue.accepting:
            _default_queue = AsyncJobQueue()
        return _default_queue


def enqueue_async_job(
    name: str,
    func: Callable[..., Any],
    *args: Any,
    metadata: dict[str, Any] | None = None,
    **kwargs: Any,
) -> AsyncJob:
    with _default_queue_lock:
        queue = _default_queue
    return queue.enqueue(name, func, *args, metadata=metadata, **kwargs)


def get_async_job_queue() -> AsyncJobQueue:
    with _default_queue_lock:
        return _default_queue


def shutdown_async_jobs() -> None:
    with _default_queue_lock:
        queue = _default_queue
    queue.shutdown()
