from __future__ import annotations

from threading import Lock


class HarnessSessionBusy(RuntimeError):
    pass


_registry_lock = Lock()
_session_locks: dict[str, Lock] = {}


def acquire_harness_session(session_id: str) -> Lock:
    normalized = str(session_id or "").strip()
    if not normalized:
        raise ValueError("session_id is required")
    with _registry_lock:
        lock = _session_locks.setdefault(normalized, Lock())
    if not lock.acquire(blocking=False):
        raise HarnessSessionBusy(
            "该会话已有一个 Harness 执行正在进行，请等待其结束后重试。"
        )
    return lock


def release_harness_session(session_id: str, lock: Lock | None) -> None:
    if lock is None:
        return
    lock.release()
