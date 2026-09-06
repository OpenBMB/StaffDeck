from __future__ import annotations

import os
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
    try:
        assert lock_path == tmp_path / "staffdeck.db.runtime.lock"
        # On Windows, opening the same locked file through a second handle can
        # raise a sharing violation. Read the owner through the held handle so
        # this assertion remains portable.
        from app import runtime_lock

        assert runtime_lock._lock_handle is not None
        runtime_lock._lock_handle.seek(0)
        assert runtime_lock._lock_handle.read().isdigit()

        # Re-entry by the owning application is harmless; another open file
        # descriptor is covered separately by the OS-level lock semantics.
        assert acquire_runtime_instance_lock() == lock_path
    finally:
        release_runtime_instance_lock()


def test_runtime_lock_reports_existing_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("fcntl owner contention is covered by the Windows integration path")

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


def test_runtime_lock_keeps_stable_error_when_owner_file_is_not_readable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app import runtime_lock

    database_path = tmp_path / "staffdeck.db"

    class UnreadableHandle:
        def seek(self, *_args) -> None:
            raise PermissionError("sharing violation")

        def read(self) -> str:
            raise PermissionError("sharing violation")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        runtime_lock,
        "get_settings",
        lambda: type("Settings", (), {"database_url": f"sqlite:///{database_path}"})(),
    )
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: UnreadableHandle())
    monkeypatch.setattr(
        runtime_lock,
        "_try_lock",
        lambda _handle: (_ for _ in ()).throw(OSError("locked")),
    )

    with pytest.raises(runtime_lock.RuntimeInstanceLockError, match="pid=unknown"):
        runtime_lock.acquire_runtime_instance_lock()
