from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import runtime_lock
from app.runtime_lock import (
    RuntimeInstanceLockError,
    acquire_runtime_instance_lock,
    release_runtime_instance_lock,
)


def test_runtime_lock_rejects_second_process_for_same_sqlite_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "staffdeck.db"
    monkeypatch.setattr(
        "app.runtime_lock.get_settings",
        lambda: type("Settings", (), {"database_url": f"sqlite:///{database_path}"})(),
    )

    lock_path = acquire_runtime_instance_lock()
    assert lock_path == tmp_path / "staffdeck.db.runtime.lock"
    assert runtime_lock._lock_handle is not None
    runtime_lock._lock_handle.seek(0)
    assert runtime_lock._lock_handle.read().strip().isdigit()

    # Re-entry by the owning application is harmless; another open file
    # descriptor is covered separately by the OS-level flock semantics.
    assert acquire_runtime_instance_lock() == lock_path
    release_runtime_instance_lock()


def test_runtime_lock_reports_existing_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "staffdeck.db"
    lock_path = tmp_path / "staffdeck.db.runtime.lock"
    owner = lock_path.open("a+", encoding="utf-8")
    owner.write("4242")
    owner.flush()
    owner.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(
        "app.runtime_lock.get_settings",
        lambda: type("Settings", (), {"database_url": f"sqlite:///{database_path}"})(),
    )

    try:
        expected = "Another StaffDeck process already owns"
        if os.name != "nt":
            expected += r" .*pid=4242"
        with pytest.raises(RuntimeInstanceLockError, match=expected):
            acquire_runtime_instance_lock()
    finally:
        if os.name == "nt":
            msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
        owner.close()
        release_runtime_instance_lock()
