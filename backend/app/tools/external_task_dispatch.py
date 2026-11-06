"""Bounded scheduling for the shared external-task tracker; no second task state machine."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import logging
import threading

from app.db.models import ExternalBusinessTask
from staffdeck_harness.contracts.runtime_services import MaintenanceIdentity
from staffdeck_harness.modules.registry import peek_registry
from staffdeck_harness.runtime.services import runtime_services

logger = logging.getLogger(__name__)
SUBMIT_WORKERS = 4
POLL_WORKERS = 2


class ExternalTaskDispatcher:
    """Reserve independent submit/poll capacity without an unbounded memory queue.

    Database scans only offer ids. Each accepted job owns its runtime session and
    holds its assembly's work lease through the existing tracker's claim/I/O.
    Unaccepted jobs remain unchanged in the database for a later scan.
    """

    def __init__(self, *, submit_workers=SUBMIT_WORKERS, poll_workers=POLL_WORKERS):
        self._limits = {"submit": submit_workers, "poll": poll_workers}
        if any(type(value) is not int or value < 1 for value in self._limits.values()):
            raise ValueError("external task concurrency must be a positive integer")
        self._executors = {
            phase: ThreadPoolExecutor(max_workers=count, thread_name_prefix=f"external-task-{phase}")
            for phase, count in self._limits.items()
        }
        self._lock = threading.RLock()
        self._active = set()
        self._counts = {"submit": 0, "poll": 0}
        self._closed = False

    def dispatch(self, db, task_id: str, phase: str) -> bool:
        if phase not in self._limits:
            raise ValueError("unknown external task phase")
        # Nothing from the scanning Session (including ORM rows) crosses threads.
        task = db.get(ExternalBusinessTask, task_id)
        if task is None:
            return False
        tenant_id = task.tenant_id
        registry = db.info.get("staffdeck_registry") or peek_registry()
        services = runtime_services(db, registry)
        registry = registry or getattr(services, "registry", None)
        namespace = services.namespace
        key = (namespace, tenant_id, task_id)
        generation = registry.generation if registry else None
        with self._lock:
            if self._closed or key in self._active or self._counts[phase] >= self._limits[phase]:
                return False
            if registry is not None and peek_registry() is not registry:
                return False
            lease = None
            lease_entered = False
            try:
                # turn_lease closes new admission during drain; work_lease keeps
                # admitted work alive after this short scheduling transaction.
                with registry.turn_lease() if registry else nullcontext():
                    lease = registry.work_lease() if registry else nullcontext()
                    lease.__enter__()
                    lease_entered = True
                self._active.add(key)
                self._counts[phase] += 1
                self._executors[phase].submit(
                    self._execute, key, phase, services, registry, generation, lease)
            except Exception as exc:
                if key in self._active:
                    self._active.remove(key)
                    self._counts[phase] -= 1
                if lease_entered:
                    lease.__exit__(None, None, None)
                if getattr(exc, "code", None) != "ENGINE_UNAVAILABLE":
                    logger.exception("external task dispatch admission failed")
                return False
        return True

    def _execute(self, key, phase, services, registry, generation, lease):
        namespace, tenant_id, task_id = key
        try:
            with self._lock:
                if self._closed:
                    return
            if registry is not None and (
                peek_registry() is not registry or registry.generation != generation
            ):
                return  # A stale offer is never replayed against a new assembly.
            with services.session(MaintenanceIdentity(tenant_id), purpose=f"external-task-{phase}") as db:
                if registry is not None:
                    db.info["staffdeck_registry"] = registry
                db.info["staffdeck_namespace"] = namespace
                task = db.get(ExternalBusinessTask, task_id)
                if task is None or task.tenant_id != tenant_id:
                    return
                with self._lock:
                    if self._closed:
                        return
                try:
                    from app.tools.external_tasks import execute_external_task
                    execute_external_task(db, task_id, phase)
                except Exception:
                    # The tracker owns lease recovery and uncertain POST state.
                    # A worker exception must never cause an automatic HTTP replay.
                    db.rollback()
                    logger.exception("external task execution failed phase=%s", phase)
        except Exception:
            logger.exception("external task session failed phase=%s", phase)
        finally:
            try:
                lease.__exit__(None, None, None)
            finally:
                with self._lock:
                    self._active.discard(key)
                    self._counts[phase] -= 1

    def close(self, *, wait=True):
        with self._lock:
            self._closed = True
        # Pending offers observe _closed and leave their DB task unchanged.
        # Already-started network I/O finishes under its own timeout and lease.
        for executor in self._executors.values():
            executor.shutdown(wait=wait, cancel_futures=False)
