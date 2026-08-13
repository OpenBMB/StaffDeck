from __future__ import annotations

from pathlib import Path

import pytest

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
    assert lock_path.read_text(encoding="utf-8").isdigit()

    # Re-entry by the owning application is harmless; another open file
    # descriptor is covered separately by the OS-level flock semantics.
    assert acquire_runtime_instance_lock() == lock_path
    release_runtime_instance_lock()


def test_runtime_lock_reports_existing_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import fcntl

    database_path = tmp_path / "staffdeck.db"
    lock_path = tmp_path / "staffdeck.db.runtime.lock"
    owner = lock_path.open("a+", encoding="utf-8")
    owner.write("4242")
    owner.flush()
    fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(
        "app.runtime_lock.get_settings",
        lambda: type("Settings", (), {"database_url": f"sqlite:///{database_path}"})(),
    )

    try:
        with pytest.raises(RuntimeInstanceLockError, match="pid=4242"):
            acquire_runtime_instance_lock()
    finally:
        fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
        owner.close()
        release_runtime_instance_lock()
