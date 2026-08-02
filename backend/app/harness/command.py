from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.harness.contracts import HarnessToolContext
from app.harness.errors import HarnessExecutionError
from app.harness.registry import HarnessRegistry

_BASH_PATH = "/bin/bash"
_SANDBOX_WORKSPACE = "/workspace"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_TIMEOUT_SECONDS = 120.0
_DEFAULT_OUTPUT_BYTES = 32 * 1024
_MAX_OUTPUT_BYTES = 128 * 1024
_MAX_COMMAND_CHARS = 8192
_READ_ONLY_SYSTEM_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    # Bind only the small runtime subset needed by dynamically linked tools.
    # The service runs as root in some deployments, so exposing all of /etc or
    # /opt would make host credentials readable from inside the sandbox.
    "/etc/alternatives",
    "/etc/ld.so.cache",
    "/etc/localtime",
    "/etc/ssl/certs",
)
_SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_DENIED_COMMANDS = {
    "bash",
    "chgrp",
    "chmod",
    "chown",
    "curl",
    "dash",
    "dd",
    "diskutil",
    "doas",
    "eval",
    "exec",
    "fdisk",
    "fish",
    "halt",
    "kill",
    "killall",
    "launchctl",
    "ln",
    "mkfs",
    "mount",
    "nc",
    "ncat",
    "open",
    "pkill",
    "poweroff",
    "reboot",
    "rm",
    "rmdir",
    "scp",
    "sftp",
    "sh",
    "shutdown",
    "shred",
    "socat",
    "source",
    "ssh",
    "sudo",
    "su",
    "systemctl",
    "telnet",
    "umount",
    "unlink",
    "wget",
    "xargs",
    "xdg-open",
    "zsh",
}
_DENIED_GIT_SUBCOMMANDS = {
    "add",
    "branch",
    "checkout",
    "clean",
    "commit",
    "config",
    "fetch",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "reset",
    "restore",
    "rm",
    "submodule",
    "switch",
    "tag",
    "worktree",
}
_DENIED_FIND_ARGUMENTS = {"-delete", "-exec", "-execdir", "-fls", "-fprint"}
_DENIED_INLINE_CODE_FLAGS = {
    "node": {"-e", "--eval", "-p", "--print"},
    "perl": {"-e", "-E"},
    "php": {"-r"},
    "python": {"-c"},
    "python3": {"-c"},
    "ruby": {"-e"},
}


class ExecCommandArguments(BaseModel):
    """Typed arguments for the isolated Bash command capability."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=_MAX_COMMAND_CHARS)
    timeout_seconds: float = Field(
        default=_DEFAULT_TIMEOUT_SECONDS,
        ge=0.1,
        le=_MAX_TIMEOUT_SECONDS,
    )
    max_output_bytes: int = Field(
        default=_DEFAULT_OUTPUT_BYTES,
        ge=128,
        le=_MAX_OUTPUT_BYTES,
    )


@dataclass(frozen=True)
class _BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    timed_out: bool
    output_truncated: bool
    duration_ms: int


@dataclass
class _CaptureBudget:
    limit: int
    captured_bytes: int = 0
    truncated: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, target: bytearray, chunk: bytes) -> None:
        with self.lock:
            remaining = max(0, self.limit - self.captured_bytes)
            accepted = min(remaining, len(chunk))
            if accepted:
                target.extend(chunk[:accepted])
                self.captured_bytes += accepted
            if accepted < len(chunk):
                self.truncated = True


@dataclass
class _StreamCapture:
    content: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0


def exec_command(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    """Execute one bounded command in a fail-closed Bubblewrap sandbox.

    The model supplies only the Bash program. Every process argument that
    creates the sandbox is trusted and passed separately to ``subprocess``.
    There is deliberately no unsandboxed fallback.
    """

    args = _as_exec_arguments(arguments)
    command = args.command.strip()
    _validate_command(command)
    workspace = _prepare_workspace(context)
    sandbox_executable = _bubblewrap_executable()
    output_limit = min(
        args.max_output_bytes,
        max(1, context.limits.max_result_bytes // 4),
    )
    argv = _bubblewrap_argv(
        sandbox_executable=sandbox_executable,
        workspace=workspace,
        command=command,
    )
    process = _run_bounded_process(
        argv,
        cwd=workspace,
        timeout_seconds=args.timeout_seconds,
        output_limit=output_limit,
    )
    status = (
        "timed_out"
        if process.timed_out
        else "completed"
        if process.returncode == 0
        else "failed"
    )
    return {
        "status": status,
        "ok": status == "completed",
        "exit_code": None if process.timed_out else process.returncode,
        "timed_out": process.timed_out,
        "stdout": process.stdout.decode("utf-8", errors="replace"),
        "stderr": process.stderr.decode("utf-8", errors="replace"),
        "stdout_bytes": process.stdout_bytes,
        "stderr_bytes": process.stderr_bytes,
        "captured_output_bytes": len(process.stdout) + len(process.stderr),
        "output_limit_bytes": output_limit,
        "output_truncated": process.output_truncated,
        "timeout_seconds": args.timeout_seconds,
        "duration_ms": process.duration_ms,
        "cwd": ".",
        "sandbox": "bubblewrap",
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
    }


def register_command_tools(registry: HarnessRegistry) -> HarnessRegistry:
    """Register high-leverage command execution on an explicit registry."""

    registry.register(
        name="exec_command",
        description=(
            "Run a bounded Bash command inside this TaskFrame workspace. "
            "The Bubblewrap sandbox has no network, exposes system directories "
            "read-only, and mounts only this workspace as writable."
        ),
        argument_model=ExecCommandArguments,
        handler=exec_command,
        side_effect="write",
    )
    return registry


def build_command_tool_registry() -> HarnessRegistry:
    return register_command_tools(HarnessRegistry())


def _as_exec_arguments(arguments: BaseModel) -> ExecCommandArguments:
    if not isinstance(arguments, ExecCommandArguments):
        raise HarnessExecutionError(
            "INVALID_ARGUMENTS",
            "Handler expected ExecCommandArguments.",
        )
    return arguments


def _prepare_workspace(context: HarnessToolContext) -> Path:
    root = context.workspace_root
    try:
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise HarnessExecutionError(
                "INVALID_WORKSPACE",
                "Harness command workspace cannot be a symbolic link.",
            )
        resolved = root.resolve(strict=True)
    except HarnessExecutionError:
        raise
    except OSError as exc:
        raise HarnessExecutionError(
            "INVALID_WORKSPACE",
            "Harness command workspace is unavailable.",
            details={"exception_type": type(exc).__name__},
        ) from exc
    if not resolved.is_dir():
        raise HarnessExecutionError(
            "INVALID_WORKSPACE",
            "Harness command workspace is not a directory.",
        )
    _reject_workspace_symlinks(resolved)
    return resolved


def _reject_workspace_symlinks(root: Path) -> None:
    try:
        for directory, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            base = Path(directory)
            for name in (*directory_names, *file_names):
                path = base / name
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    raise HarnessExecutionError(
                        "SYMLINK_NOT_ALLOWED",
                        "Symbolic links are not allowed in Harness command workspaces.",
                        details={"path": path.relative_to(root).as_posix()},
                    )
    except HarnessExecutionError:
        raise
    except OSError as exc:
        raise HarnessExecutionError(
            "INVALID_WORKSPACE",
            "Harness command workspace could not be inspected safely.",
            details={"exception_type": type(exc).__name__},
        ) from exc


def _bubblewrap_executable() -> str:
    if not sys.platform.startswith("linux"):
        raise HarnessExecutionError(
            "SANDBOX_UNAVAILABLE",
            "exec_command requires Bubblewrap on Linux; no unsafe fallback is allowed.",
        )
    executable = shutil.which("bwrap")
    if not executable:
        raise HarnessExecutionError(
            "SANDBOX_UNAVAILABLE",
            "Bubblewrap is unavailable; exec_command is disabled fail-closed.",
        )
    path = Path(executable)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise HarnessExecutionError(
            "SANDBOX_UNAVAILABLE",
            "Bubblewrap executable is not a trusted executable file.",
        )
    if not Path(_BASH_PATH).is_file():
        raise HarnessExecutionError(
            "SANDBOX_UNAVAILABLE",
            "The sandboxed Bash runtime is unavailable.",
        )
    return str(path.resolve(strict=True))


def _bubblewrap_argv(
    *,
    sandbox_executable: str,
    workspace: Path,
    command: str,
) -> list[str]:
    argv = [
        sandbox_executable,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--unshare-net",
        "--cap-drop",
        "ALL",
        "--clearenv",
    ]
    for raw_path in _READ_ONLY_SYSTEM_PATHS:
        path = Path(raw_path)
        if path.is_symlink():
            argv.extend(("--symlink", os.readlink(path), raw_path))
        elif path.exists():
            argv.extend(("--ro-bind", raw_path, raw_path))
    argv.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--dir",
            _SANDBOX_WORKSPACE,
            "--bind",
            str(workspace),
            _SANDBOX_WORKSPACE,
            # Bubblewrap's root is otherwise an anonymous tmpfs. Remount only
            # that mount read-only; the nested workspace bind remains writable.
            "--remount-ro",
            "/",
            "--chdir",
            _SANDBOX_WORKSPACE,
            "--setenv",
            "HOME",
            _SANDBOX_WORKSPACE,
            "--setenv",
            "PWD",
            _SANDBOX_WORKSPACE,
            "--setenv",
            "TMPDIR",
            _SANDBOX_WORKSPACE,
            "--setenv",
            "PATH",
            _SANDBOX_PATH,
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "LC_ALL",
            "C.UTF-8",
            "--",
            _BASH_PATH,
            "--noprofile",
            "--norc",
            "-c",
            command,
        )
    )
    return argv


def _run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    output_limit: int,
) -> _BoundedProcessResult:
    started = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd),
        env={
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        _terminate_process_group(process)
        raise HarnessExecutionError(
            "COMMAND_START_FAILED",
            "Sandbox process did not expose bounded output streams.",
        )

    budget = _CaptureBudget(limit=max(1, output_limit))
    stdout_capture = _StreamCapture()
    stderr_capture = _StreamCapture()
    threads = (
        threading.Thread(
            target=_drain_stream,
            args=(process.stdout, stdout_capture, budget),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr_capture, budget),
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise HarnessExecutionError(
                "COMMAND_TERMINATION_FAILED",
                "Timed-out sandbox process could not be terminated.",
            ) from exc
    finally:
        if process.poll() is not None:
            _terminate_process_group(process)
        for thread in threads:
            thread.join(timeout=2.0)
        for stream, thread in zip((process.stdout, process.stderr), threads, strict=True):
            if thread.is_alive():
                stream.close()
                thread.join(timeout=0.2)

    return _BoundedProcessResult(
        returncode=int(process.returncode if process.returncode is not None else -1),
        stdout=bytes(stdout_capture.content),
        stderr=bytes(stderr_capture.content),
        stdout_bytes=stdout_capture.total_bytes,
        stderr_bytes=stderr_capture.total_bytes,
        timed_out=timed_out,
        output_truncated=budget.truncated,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
    )


def _drain_stream(
    stream: BinaryIO,
    capture: _StreamCapture,
    budget: _CaptureBudget,
) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            capture.total_bytes += len(chunk)
            budget.append(capture.content, chunk)
    except (OSError, ValueError):
        return


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if process.poll() is None:
            process.kill()


def _validate_command(command: str) -> None:
    if not command or not command.strip():
        raise _command_denied("Command cannot be empty.")
    if "\x00" in command:
        raise _command_denied("Command cannot contain a null byte.")
    if "\n" in command or "\r" in command:
        raise _command_denied("Multiline shell programs are not allowed.")
    if "$" in command or "`" in command:
        raise _command_denied("Shell expansion and command substitution are not allowed.")
    if "<(" in command or ">(" in command:
        raise _command_denied("Process substitution is not allowed.")

    try:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars=";&|<>()",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise _command_denied("Command has invalid shell quoting.") from exc
    if not tokens:
        raise _command_denied("Command cannot be empty.")

    words = [token for token in tokens if not _is_shell_operator(token)]
    for token in tokens:
        if token in {"<<", "<<<", "<&", ">&", "&>", "|&"}:
            raise _command_denied("Unsafe shell redirection is not allowed.")
        if "&" in token and token != "&&":
            raise _command_denied("Background processes are not allowed.")
    for token in words:
        _validate_path_token(token)
        executable = PurePosixPath(token).name
        if executable in _DENIED_COMMANDS:
            raise _command_denied(f"Dangerous command is not allowed: {executable}")

    _validate_command_specific_arguments(words)


def _validate_command_specific_arguments(words: list[str]) -> None:
    for index, token in enumerate(words):
        executable = PurePosixPath(token).name
        if executable == "git" and index + 1 < len(words):
            denied = {
                word.casefold() for word in words[index + 1 :]
            } & _DENIED_GIT_SUBCOMMANDS
            if denied:
                subcommand = sorted(denied)[0]
                raise _command_denied(
                    f"State-changing git command is not allowed: {subcommand}"
                )
        if executable == "find":
            remaining = set(words[index + 1 :])
            if remaining & _DENIED_FIND_ARGUMENTS:
                raise _command_denied("State-changing find arguments are not allowed.")
        denied_flags = _DENIED_INLINE_CODE_FLAGS.get(executable)
        if denied_flags and index + 1 < len(words):
            if words[index + 1] in denied_flags:
                raise _command_denied(
                    f"Inline code execution is not allowed for {executable}."
                )


def _validate_path_token(token: str) -> None:
    candidates = [token]
    if "=" in token:
        candidates.extend(part for part in token.split("=")[1:] if part)
    for candidate in candidates:
        normalized = candidate.replace("\\", "/")
        if PureWindowsPath(candidate).drive:
            raise _command_denied("Drive-qualified paths are not allowed.")
        if normalized.startswith("/") or normalized.startswith("~"):
            raise _command_denied("Absolute and home-relative paths are not allowed.")
        if ".." in PurePosixPath(normalized).parts:
            raise _command_denied("Parent-directory traversal is not allowed.")
        if ".harness-trash" in PurePosixPath(normalized).parts:
            raise _command_denied("Harness internal paths are not accessible.")
        slash_index = normalized.find("/")
        if slash_index > 0 and normalized[:slash_index].startswith("-"):
            raise _command_denied("Options containing absolute paths are not allowed.")


def _is_shell_operator(token: str) -> bool:
    return bool(token) and all(character in ";&|<>()" for character in token)


def _command_denied(message: str) -> HarnessExecutionError:
    return HarnessExecutionError("COMMAND_DENIED", message)


__all__ = [
    "ExecCommandArguments",
    "build_command_tool_registry",
    "exec_command",
    "register_command_tools",
]
