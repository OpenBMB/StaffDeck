from __future__ import annotations

import os
import signal
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Sequence


class ManagedProcessError(RuntimeError):
    pass


class _WindowsJob:
    """Windows Job Object configured to terminate every descendant on close."""

    def __init__(self, handle: Any, kernel32: Any) -> None:
        self._handle = handle
        self._kernel32 = kernel32

    @classmethod
    def assign(cls, proc: subprocess.Popen[Any]) -> _WindowsJob:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not configured:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(proc._handle)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        return cls(handle, kernel32)

    def resume(self, proc: subprocess.Popen[Any]) -> None:
        import ctypes
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = ntdll.NtResumeProcess(wintypes.HANDLE(proc._handle))
        if status < 0:
            raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}")

    def close(self) -> None:
        if not self._handle:
            return
        self._kernel32.TerminateJobObject(self._handle, 1)
        self._kernel32.CloseHandle(self._handle)
        self._handle = None


def _popen_platform_options(platform_name: str) -> dict[str, Any]:
    if platform_name == "nt":
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            )
        }
    return {"start_new_session": True}


@dataclass
class ManagedProcess:
    process: subprocess.Popen[Any]
    platform_name: str
    _windows_job: _WindowsJob | None = None
    _closed: bool = False

    @classmethod
    def start(
        cls,
        argv: Sequence[str],
        *,
        platform_name: str | None = None,
        **popen_kwargs: Any,
    ) -> ManagedProcess:
        platform = platform_name or os.name
        proc = subprocess.Popen(
            list(argv),
            **popen_kwargs,
            **_popen_platform_options(platform),
        )
        job: _WindowsJob | None = None
        if platform == "nt":
            try:
                job = _WindowsJob.assign(proc)
                job.resume(proc)
            except OSError as exc:
                if job is not None:
                    job.close()
                _terminate_process_tree(proc, platform)
                raise ManagedProcessError(
                    f"Windows 进程隔离初始化失败，子进程未启动：{exc}"
                ) from exc
        return cls(process=proc, platform_name=platform, _windows_job=job)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._windows_job is not None:
            self._windows_job.close()
            self._windows_job = None
        _terminate_process_tree(self.process, self.platform_name)


def _terminate_process_tree(proc: subprocess.Popen[Any], platform_name: str) -> None:
    if platform_name == "nt" and proc.poll() is None:
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    elif platform_name != "nt":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)

    if proc.poll() is None:
        with suppress(OSError):
            proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        if platform_name != "nt":
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        with suppress(OSError):
            proc.kill()
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=1)

    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            with suppress(OSError, ValueError):
                stream.close()
