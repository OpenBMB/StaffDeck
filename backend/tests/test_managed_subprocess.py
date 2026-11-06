from __future__ import annotations

from typing import Any

import pytest

from app.security import managed_subprocess
from app.security.managed_subprocess import ManagedProcess, ManagedProcessError


class _FakeProcess:
    pid = 123
    _handle = 456
    stdin = None
    stdout = None
    stderr = None

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0


def test_windows_start_is_suspended_before_job_assignment_and_resume(monkeypatch) -> None:
    events: list[str] = []
    popen_kwargs: dict[str, Any] = {}
    proc = _FakeProcess()

    class FakeJob:
        @classmethod
        def assign(cls, assigned_proc):  # noqa: ANN001
            assert assigned_proc is proc
            events.append("assign")
            return cls()

        def resume(self, resumed_proc) -> None:  # noqa: ANN001
            assert resumed_proc is proc
            events.append("resume")

        def close(self) -> None:
            events.append("job-close")

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        assert argv == ["node", "index.js"]
        events.append("popen-suspended")
        popen_kwargs.update(kwargs)
        return proc

    monkeypatch.setattr(managed_subprocess, "_WindowsJob", FakeJob)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", fake_popen)

    managed = ManagedProcess.start(["node", "index.js"], platform_name="nt")

    assert events == ["popen-suspended", "assign", "resume"]
    assert popen_kwargs["creationflags"] & 0x00000004
    assert "start_new_session" not in popen_kwargs
    assert managed.isolation_mode == "windows_job"
    managed.close()
    managed.close()
    assert events.count("job-close") == 1


def test_windows_job_failure_terminates_suspended_process(monkeypatch) -> None:
    proc = _FakeProcess()
    terminated: list[tuple[object, str]] = []

    class FailingJob:
        @classmethod
        def assign(cls, assigned_proc):  # noqa: ANN001
            assert assigned_proc is proc
            raise OSError("job denied")

    monkeypatch.setattr(managed_subprocess, "_WindowsJob", FailingJob)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(
        managed_subprocess,
        "_terminate_process_tree",
        lambda target, platform: terminated.append((target, platform)),
    )

    with pytest.raises(ManagedProcessError, match="子进程未启动"):
        ManagedProcess.start(["node", "index.js"], platform_name="nt")

    assert terminated == [(proc, "nt")]


@pytest.mark.parametrize("winerror", [5, 50])
def test_windows_nested_job_failure_falls_back_to_process_group(monkeypatch, winerror) -> None:
    proc = _FakeProcess()
    events: list[str] = []

    class NestedJobRejected:
        @classmethod
        def assign(cls, assigned_proc):  # noqa: ANN001
            assert assigned_proc is proc
            raise managed_subprocess._WindowsJobAssignmentError(
                winerror,
                "nested job rejected",
                parent_in_job=True,
                child_in_job=True,
            )

    monkeypatch.setattr(managed_subprocess, "_WindowsJob", NestedJobRejected)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(
        managed_subprocess,
        "_resume_windows_process",
        lambda target: events.append(f"resume:{target.pid}"),
    )
    monkeypatch.setattr(
        managed_subprocess,
        "_terminate_process_tree",
        lambda target, platform: events.append(f"terminate:{target.pid}:{platform}"),
    )

    managed = ManagedProcess.start(["wecom-cli"], platform_name="nt")

    assert managed.isolation_mode == "windows_process_group_fallback"
    assert managed.isolation_details == {
        "fallback_reason": "nested_job_assignment_rejected",
        "windows_error_code": winerror,
        "parent_in_job": True,
        "child_in_job": True,
        "cleanup": "taskkill_tree",
    }
    assert events == ["resume:123"]
    managed.close()
    assert events == ["resume:123", "terminate:123:nt"]


def test_windows_assignment_failure_without_existing_job_still_fails_closed(monkeypatch) -> None:
    proc = _FakeProcess()
    terminated: list[tuple[object, str]] = []

    class AssignmentRejected:
        @classmethod
        def assign(cls, assigned_proc):  # noqa: ANN001
            assert assigned_proc is proc
            raise managed_subprocess._WindowsJobAssignmentError(
                5,
                "assignment rejected",
                parent_in_job=False,
                child_in_job=False,
            )

    monkeypatch.setattr(managed_subprocess, "_WindowsJob", AssignmentRejected)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(
        managed_subprocess,
        "_terminate_process_tree",
        lambda target, platform: terminated.append((target, platform)),
    )

    with pytest.raises(ManagedProcessError, match="子进程未启动"):
        ManagedProcess.start(["wecom-cli"], platform_name="nt")

    assert terminated == [(proc, "nt")]


def test_posix_start_uses_a_new_session(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    terminated: list[tuple[object, str]] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        managed_subprocess,
        "_terminate_process_tree",
        lambda target, platform: terminated.append((target, platform)),
    )

    managed = ManagedProcess.start(["python", "server.py"], platform_name="posix")

    assert captured["start_new_session"] is True
    assert "creationflags" not in captured
    assert managed.isolation_mode == "posix_session"
    managed.close()
    assert terminated == [(managed.process, "posix")]
