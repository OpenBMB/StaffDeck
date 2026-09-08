from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any

from app.db.models import new_id, utc_now

logger = logging.getLogger(__name__)

AsyncJobStatus = str

# ---- Redis 持久化层（P1-4：重启不丢任务） ----
#
# 每个可序列化的任务（模块级函数 + JSON 可序列化参数）在 enqueue 时写一份
# 记录到 Redis：记录 hash ``staffdeck:jobs:record:{id}`` + pending List
# ``staffdeck:jobs:pending``。执行开始置 running，终态删除。
# 进程重启后 start_async_jobs() 扫描 pending List：
#   - pending   → 重新提交本地线程池执行（at-least-once；run_job 类任务内部
#                 有 DB 租约 claim，天然幂等防重）；
#   - running   → 标记 failed（「进程重启中断」），不自动重跑有副作用的任务。
# Redis 不可用时以下所有函数均为 no-op，队列行为与纯内存版完全一致。

_JOBS_RECORD_PREFIX = "staffdeck:jobs:record:"
_JOBS_PENDING_KEY = "staffdeck:jobs:pending"
_JOBS_RECORD_TTL_SECONDS = 24 * 3600
_JOBS_PENDING_MAX = 1000


def _get_redis():
    from app.redis_client import get_redis

    return get_redis()


def _serialize_callable(func: Callable[..., Any]) -> str | None:
    """模块级函数 → "module:qualname"；lambda/局部函数/bound method 返回 None。"""
    if not inspect.isfunction(func):
        return None
    module = getattr(func, "__module__", None)
    qualname = getattr(func, "__qualname__", None)
    if not module or not qualname or "<locals>" in qualname:
        return None
    return f"{module}:{qualname}"


def _deserialize_callable(ref: str) -> Callable[..., Any] | None:
    import importlib

    try:
        module_name, qualname = ref.split(":", 1)
        obj: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            obj = getattr(obj, part)
        if callable(obj):
            return obj
    except Exception:
        return None
    return None


def _persist_job(job: AsyncJob, func: Callable[..., Any], args: tuple, kwargs: dict) -> bool:
    client = _get_redis()
    if client is None:
        return False
    func_ref = _serialize_callable(func)
    if func_ref is None:
        return False
    try:
        payload = json.dumps(
            {
                "args": list(args),
                "kwargs": kwargs,
            },
            ensure_ascii=False,
            default=str,
        )
        # 校验参数可 JSON 序列化（payload 解回后语义一致才落 Redis）
        json.loads(payload)
        record = json.dumps(
            {
                "id": job.id,
                "name": job.name,
                "func": func_ref,
                "args_payload": payload,
                "metadata": job.metadata,
                "state": "pending",
                "created_at": job.created_at.isoformat() if job.created_at else None,
            },
            ensure_ascii=False,
        )
        pipe = client.pipeline()
        pipe.set(_JOBS_RECORD_PREFIX + job.id, record, ex=_JOBS_RECORD_TTL_SECONDS)
        pipe.lpush(_JOBS_PENDING_KEY, job.id)
        pipe.ltrim(_JOBS_PENDING_KEY, 0, _JOBS_PENDING_MAX - 1)
        pipe.execute()
        return True
    except Exception:
        # 参数不可序列化/Redis 故障 → 该任务退化为纯内存语义
        return False


def _mark_job_running(job_id: str) -> None:
    client = _get_redis()
    if client is None:
        return
    try:
        raw = client.get(_JOBS_RECORD_PREFIX + job_id)
        if raw is None:
            return
        record = json.loads(raw)
        record["state"] = "running"
        client.set(_JOBS_RECORD_PREFIX + job_id, json.dumps(record, ensure_ascii=False),
                   ex=_JOBS_RECORD_TTL_SECONDS)
    except Exception:
        pass


def _clear_job(job_id: str) -> None:
    client = _get_redis()
    if client is None:
        return
    try:
        client.delete(_JOBS_RECORD_PREFIX + job_id)
        client.lrem(_JOBS_PENDING_KEY, 0, job_id)
    except Exception:
        pass


def _fail_job_record(job_id: str, error: str) -> None:
    """running 中断（重启）→ 记录为失败并出队，不自动重跑。"""
    client = _get_redis()
    if client is None:
        return
    try:
        raw = client.get(_JOBS_RECORD_PREFIX + job_id)
        client.lrem(_JOBS_PENDING_KEY, 0, job_id)
        if raw is not None:
            record = json.loads(raw)
            record["state"] = "failed"
            record["error"] = error
            client.set(_JOBS_RECORD_PREFIX + job_id, json.dumps(record, ensure_ascii=False),
                       ex=3600)
    except Exception:
        pass


def _recover_jobs(queue: AsyncJobQueue) -> int:
    """启动时恢复 Redis 中残留的任务；返回恢复数量。任何异常静默忽略。"""
    client = _get_redis()
    if client is None:
        return 0
    recovered = 0
    try:
        ids = client.lrange(_JOBS_PENDING_KEY, 0, -1)
    except Exception:
        return 0
    seen: set[str] = set()
    for raw_id in ids:
        job_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
        if job_id in seen:
            continue
        seen.add(job_id)
        try:
            raw = client.get(_JOBS_RECORD_PREFIX + job_id)
        except Exception:
            continue
        if raw is None:
            try:
                client.lrem(_JOBS_PENDING_KEY, 0, job_id)
            except Exception:
                pass
            continue
        try:
            record = json.loads(raw)
            state = str(record.get("state") or "pending")
            if state != "pending":
                _fail_job_record(job_id, "Job interrupted by a service restart")
                continue
            func = _deserialize_callable(str(record.get("func") or ""))
            if func is None:
                _fail_job_record(job_id, "Job function no longer importable after restart")
                continue
            args_payload = json.loads(str(record.get("args_payload") or "[]"))
            args = tuple(args_payload.get("args") or [])
            kwargs = dict(args_payload.get("kwargs") or {})
            created_at = record.get("created_at")
            job = AsyncJob(
                id=job_id,
                name=str(record.get("name") or "recovered"),
                status="queued",
                metadata=dict(record.get("metadata") or {}),
                created_at=datetime.fromisoformat(created_at) if created_at else utc_now(),
            )
            if queue.adopt_recovered(job, func, args, kwargs):
                recovered += 1
        except Exception:
            continue
    return recovered


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
        # 先落 Redis 再提交执行：保证「已接受的任务」一定有持久化记录
        # （先提交后落盘的话，秒完成的任务会在清记录后又被写回 pending）
        persisted = _persist_job(job, func, args, kwargs)
        try:
            future = self._executor.submit(self._run_job, job.id, func, args, kwargs)
        except Exception:
            # A rejected submission was never accepted. Do not expose a
            # phantom queued handle or let it consume history capacity.
            with self._lock:
                self._jobs.pop(job.id, None)
            if persisted:
                _clear_job(job.id)
            raise
        with self._lock:
            self._futures[job.id] = future
        future.add_done_callback(lambda completed, job_id=job.id: self._finish_future(job_id, completed))
        return job

    def adopt_recovered(
        self,
        job: AsyncJob,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> bool:
        """重启恢复：把 Redis 里残留的 pending 任务重新提交到本地线程池。

        不再走 enqueue（避免重复持久化）；返回 False 表示队列已关闭。
        """
        with self._lock:
            if not self._accepting or job.id in self._jobs:
                return False
            self._jobs[job.id] = job
            self._trim_history_locked()
        try:
            self._executor.submit(self._run_job, job.id, func, args, kwargs)
        except Exception:
            with self._lock:
                self._jobs.pop(job.id, None)
            _fail_job_record(job.id, "Executor rejected recovered job")
            return False
        return True

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
            cancelled_ids = [
                job.id for job in self._jobs.values() if job.status == "queued"
            ]
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
        # 优雅关闭时被取消的排队任务同步清掉 Redis 记录（正常完成的已在
        # _run_job 终态清理；仍 running 的属于异常中断，留给下次启动恢复判定）
        for job_id in cancelled_ids:
            _clear_job(job_id)

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
        _mark_job_running(job_id)
        self._update(job_id, status="running", started_at=utc_now())
        try:
            func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - background jobs must never crash the request path.
            self._update(job_id, status="failed", finished_at=utc_now(), error=str(exc))
            _clear_job(job_id)
            return
        self._update(job_id, status="succeeded", finished_at=utc_now(), error=None)
        _clear_job(job_id)

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
        queue = _default_queue
    # 重启恢复：Redis 里残留的 pending 任务重新提交执行（任何异常静默忽略，
    # 不阻塞应用启动）
    try:
        recovered = _recover_jobs(queue)
        if recovered:
            logger.info("已从 Redis 恢复 %d 个未完成异步任务", recovered)
    except Exception:  # noqa: BLE001 - 恢复失败不影响启动
        logger.exception("异步任务恢复失败")
    return queue


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
