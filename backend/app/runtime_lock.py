from __future__ import annotations

import os
from pathlib import Path
from typing import IO

from app.config import get_settings
from app.db.database_path import sqlite_database_path


class RuntimeInstanceLockError(RuntimeError):
    """Raised when another StaffDeck process already owns the SQLite runtime."""


_lock_handle: IO[str] | None = None


def _try_lock(handle: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write("0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire_runtime_instance_lock() -> Path | None:
    """Keep one application process per SQLite database.

    Uvicorn runs FastAPI startup hooks before it binds the listening socket. A
    second service targeting the same port can therefore execute recovery jobs
    and other startup side effects before the bind eventually fails. Holding a
    database-scoped process lock prevents that losing instance from reaching
    those hooks.
    """

    global _lock_handle
    if _lock_handle is not None:
        return Path(_lock_handle.name)

    database_path = sqlite_database_path(get_settings().database_url)
    if database_path is None:
        return None

    lock_path = database_path.with_name(f"{database_path.name}.runtime.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        _try_lock(handle)
    except OSError as exc:
        handle.seek(0)
        owner = handle.read().strip() or "unknown"
        handle.close()
        raise RuntimeInstanceLockError(
            f"Another StaffDeck process already owns {database_path} (pid={owner})."
        ) from exc

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_handle = handle
    return lock_path


def release_runtime_instance_lock() -> None:
    global _lock_handle
    handle = _lock_handle
    if handle is None:
        return
    _lock_handle = None
    try:
        _unlock(handle)
    finally:
        handle.close()
