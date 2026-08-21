from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.db.database_path import sqlite_database_path
from app.harness.contracts import HarnessToolContext
from app.harness.errors import HarnessExecutionError
from app.harness.execution_context import SANDBOX_WORKSPACE, SandboxExecutionContext
from app.harness.registry import HarnessRegistry
from app.harness.sandbox import (
    available_backend,
    ensure_backend_usable,
    require_backend,
    resolve_srt,
)
from app.security.managed_subprocess import ManagedProcess, ManagedProcessError

_BASH_PATH = "/bin/bash"
_WINDOWS_POWERSHELL = "powershell.exe"
_WINDOWS_PROFILE_EXPANSION = re.compile(
    r"(?i)(?:\$env:(?:userprofile|homedrive|homepath|appdata|localappdata|programdata)\b"
    r"|\$(?:userprofile|home|profile|pshome|psscriptroot)\b"
    r"|%[a-z_][a-z0-9_]*%)"
)
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
    "/etc/hosts",
    "/etc/ld.so.cache",
    "/etc/localtime",
    "/etc/nsswitch.conf",
    "/etc/resolv.conf",
    "/etc/ssl/certs",
)
_SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
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
    """Execute one bounded platform-shell program in the TaskFrame workspace.

    The model supplies only the shell program. Every process argument that
    creates the sandbox is trusted and passed separately to ``subprocess``.
    An administrator decides whether the extra OS sandbox is enabled. When it
    is disabled, the process still receives bounded time/output and a dedicated
    workspace, but runs directly on the host.
    """

    args = _as_exec_arguments(arguments)
    command = args.command.strip()
    # Models use the stable /workspace alias across every sandbox backend.
    # Normalize that one compatibility alias before validation. Other absolute
    # paths remain unchanged and are honored according to process permissions
    # and the administrator-selected OS sandbox policy.
    validation_command = _command_for_sandbox_workspace(command, "unsandboxed")
    if sys.platform == "win32":
        _validate_windows_command(validation_command)
    else:
        _validate_command(validation_command)
    workspace = _prepare_workspace(context)
    backend = available_backend() if context.sandbox_enabled else "unsandboxed"
    # Keep the existing Bubblewrap seam patchable for unit tests and Linux
    # deployments where the executable is provisioned outside PATH lookup.
    if backend is None:
        try:
            _bubblewrap_executable()
        except HarnessExecutionError:
            pass
        else:
            backend = "bubblewrap"
    if backend is None:
        backend = "unsandboxed" if sys.platform == "win32" else require_backend()
    elif backend == "srt":
        try:
            ensure_backend_usable(backend)
        except HarnessExecutionError:
            if sys.platform != "win32":
                raise
            backend = "unsandboxed"
    else:
        ensure_backend_usable(backend)
    command = _command_for_sandbox_workspace(command, backend)
    output_limit = min(
        args.max_output_bytes,
        max(1, context.limits.max_result_bytes // 4),
    )
    settings_path: Path | None = None
    sandbox_temp: tempfile.TemporaryDirectory[str] | None = None
    sandbox_temp_path: Path | None = None
    try:
        if backend == "unsandboxed":
            argv = _unsandboxed_argv(command)
        elif backend == "srt":
            sandbox_temp = _create_srt_temp_directory()
            sandbox_temp_path = Path(sandbox_temp.name)
            settings_path = _write_srt_settings(
                workspace,
                network_mode=context.sandbox_network_mode,
                allowed_domains=context.sandbox_allowed_domains,
                sandbox_temp=sandbox_temp_path,
            )
            argv = _srt_argv(settings_path=settings_path, command=command)
        else:
            argv = _bubblewrap_argv(
                sandbox_executable=_bubblewrap_executable()
                if backend == "bubblewrap"
                else _seatbelt_executable(),
                workspace=workspace,
                command=command,
                backend=backend,
                env=None,
                network_mode=context.sandbox_network_mode,
            )
        process_kwargs: dict[str, Any] = {
            "cwd": workspace,
            "timeout_seconds": args.timeout_seconds,
            "output_limit": output_limit,
        }
        if sandbox_temp_path is not None:
            process_kwargs["env"] = {"TMPDIR": str(sandbox_temp_path)}
        process = _run_bounded_process(argv, **process_kwargs)
    finally:
        if settings_path is not None:
            settings_path.unlink(missing_ok=True)
        if sandbox_temp is not None:
            sandbox_temp.cleanup()
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
        "cwd": SANDBOX_WORKSPACE,
        "sandbox": backend,
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
    }


def _command_for_sandbox_workspace(command: str, backend: str) -> str:
    """Make the stable model-visible /workspace path work on non-mount sandboxes."""

    if sys.platform == "win32":
        # Models sometimes translate the stable /workspace alias into a
        # Windows-looking path. Keep the alias portable without rewriting any
        # other user-supplied absolute path.
        command = re.sub(
            r"(?i)(?<![A-Za-z0-9_.-])[A-Za-z]:[\\/]workspace"
            r"(?=[\\/\s]|$|[\"'])",
            ".",
            command,
        )
        command = re.sub(
            r"(?i)(?<![:A-Za-z0-9_.-])[\\/]{1,2}workspace"
            r"(?=[\\/\s]|$|[\"'])",
            ".",
            command,
        )
    if backend == "bubblewrap":
        return command
    return re.sub(r"(?<![A-Za-z0-9_.-])/workspace(?=/|\s|$|[\"'])", ".", command)


def register_command_tools(registry: HarnessRegistry) -> HarnessRegistry:
    """Register high-leverage command execution on an explicit registry."""

    registry.register(
        name="exec_command",
        description=(
            "Run a bounded platform shell script, including multiple newline-separated "
            "statements, inside this TaskFrame workspace. "
            "Linux and macOS use Bash; Windows uses PowerShell. The selected OS "
            "sandbox makes only this workspace writable and applies the tenant's "
            "configured network policy. On Windows do not use Bash syntax (heredoc, "
            "python3, py -3, or bash chaining); use PowerShell statements. In a "
            "packaged Windows build, python/python3 resolve to the bundled runtime; "
            "in source deployments, use the General Skill Python runtime. Relative "
            "paths start in the TaskFrame workspace; absolute paths are accepted and "
            "remain subject to OS permissions and the configured sandbox policy."
        ),
        argument_model=ExecCommandArguments,
        handler=exec_command,
        side_effect="write",
    )
    return registry


def run_sandboxed_process(
    *,
    workspace: Path,
    argv: Sequence[str],
    stdin_bytes: bytes = b"",
    stdin_json: dict[str, Any] | None = None,
    stdin_path_keys: tuple[str, ...] = (),
    cwd: Path | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = _DEFAULT_OUTPUT_BYTES,
    network_mode: str = "all",
    allowed_domains: tuple[str, ...] = (),
    sandbox_enabled: bool = True,
    env: dict[str, str] | None = None,
    env_path_keys: tuple[str, ...] = (),
    is_cancelled: Callable[[], bool] | None = None,
) -> _BoundedProcessResult:
    """Run a fixed argv through the same OS sandbox as ``exec_command``."""
    if not argv or any("\x00" in str(item) for item in argv):
        raise HarnessExecutionError(
            "INVALID_ARGUMENTS", "Sandbox argv cannot be empty or contain NUL bytes."
        )
    workspace = _prepare_workspace(
        HarnessToolContext(run_id="sandbox-process", workspace_root=workspace)
    )
    if stdin_json is not None and stdin_bytes:
        raise HarnessExecutionError("INVALID_ARGUMENTS", "Use stdin_json or stdin_bytes, not both.")
    # ``argv`` is assembled by the trusted runner, not supplied by the model.
    # Runtime interpreters and materialized scripts are intentionally absolute
    # paths; shell command validation belongs to ``exec_command`` only.
    backend = available_backend() if sandbox_enabled else "unsandboxed"
    if backend is None:
        try:
            _bubblewrap_executable()
        except HarnessExecutionError:
            pass
        else:
            backend = "bubblewrap"
    if backend is None:
        backend = "unsandboxed" if sys.platform == "win32" else require_backend()
    elif backend == "srt":
        try:
            ensure_backend_usable(backend)
        except HarnessExecutionError:
            if sys.platform != "win32":
                raise
            backend = "unsandboxed"
    if backend == "unsandboxed":
        process_argv = [str(item) for item in argv]
        process_env = dict(env or {})
        host_cwd = (cwd or workspace).resolve()
    else:
        execution = SandboxExecutionContext.create(workspace, backend)
        process_argv = execution.map_argv([str(item) for item in argv])
        process_env = execution.map_env(env, path_keys=env_path_keys)
        host_cwd = execution.host_cwd(cwd)
    if stdin_json is not None:
        mapped_payload = (
            stdin_json
            if backend == "unsandboxed"
            else execution.map_payload(stdin_json, path_keys=stdin_path_keys)
        )
        stdin_bytes = json.dumps(mapped_payload, ensure_ascii=False).encode("utf-8")
    command = (
        subprocess.list2cmdline(process_argv)
        if sys.platform == "win32"
        else shlex.join(process_argv)
    )
    settings_path: Path | None = None
    sandbox_temp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if backend == "unsandboxed":
            sandbox_argv = process_argv
        elif backend == "srt":
            sandbox_temp = _create_srt_temp_directory()
            sandbox_temp_path = Path(sandbox_temp.name)
            process_env["TMPDIR"] = str(sandbox_temp_path)
            settings_path = _write_srt_settings(
                workspace,
                network_mode=network_mode,
                allowed_domains=allowed_domains,
                sandbox_temp=sandbox_temp_path,
            )
            sandbox_argv = _srt_argv(settings_path=settings_path, command=command)
        else:
            runtime_roots: list[Path] = []
            for item in argv:
                candidate = Path(str(item))
                if candidate.is_absolute() and candidate.exists():
                    # Expose only the runtime installation containing a fixed
                    # interpreter; the generated runner itself lives inside
                    # the workspace and needs no extra mount.
                    if candidate.name in {"python", "python3", "python.exe", "node", "node.exe"}:
                        resolved_candidate = candidate.resolve()
                        runtime_roots.extend(
                            (candidate.parent.parent, resolved_candidate.parent.parent)
                        )
            sandbox_argv = _bubblewrap_argv(
                sandbox_executable=(
                    _bubblewrap_executable()
                    if backend == "bubblewrap"
                    else _seatbelt_executable()
                ),
                workspace=workspace,
                command=command,
                backend=backend,
                env=process_env,
                extra_readonly_paths=runtime_roots,
                network_mode=network_mode,
                sandbox_cwd=execution.sandbox_cwd(cwd),
            )
        process_kwargs: dict[str, Any] = {
            "cwd": host_cwd,
            "timeout_seconds": timeout_seconds,
            "output_limit": output_limit,
            "stdin_bytes": stdin_bytes,
            "env": process_env,
        }
        if is_cancelled is not None:
            process_kwargs["is_cancelled"] = is_cancelled
        return _run_bounded_process(sandbox_argv, **process_kwargs)
    finally:
        if settings_path is not None:
            settings_path.unlink(missing_ok=True)
        if sandbox_temp is not None:
            sandbox_temp.cleanup()


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


def _seatbelt_executable() -> str:
    if sys.platform != "darwin":
        raise HarnessExecutionError("SANDBOX_UNAVAILABLE", "Seatbelt is only available on macOS.")
    executable = shutil.which("sandbox-exec")
    if not executable or not Path(executable).is_file():
        raise HarnessExecutionError("SANDBOX_UNAVAILABLE", "macOS sandbox-exec is unavailable.")
    return str(Path(executable).resolve(strict=True))


def _write_srt_settings(
    workspace: Path,
    *,
    network_mode: str = "all",
    allowed_domains: tuple[str, ...] = (),
    sandbox_temp: Path | None = None,
) -> Path:
    if network_mode not in {"all", "allowlist", "deny"}:
        raise HarnessExecutionError("SANDBOX_POLICY_INVALID", "Unknown sandbox network policy.")
    if network_mode == "deny":
        domains: list[str] = []
    elif network_mode == "allowlist":
        domains = [item.strip() for item in allowed_domains if item.strip()]
    else:
        if not _srt_supports_allow_all():
            raise HarnessExecutionError(
                "SANDBOX_POLICY_UNSUPPORTED",
                "The installed SRT runtime cannot enforce unrestricted networking; "
                "repair the bundled runtime.",
            )
        domains = []
    network = {
        "allowedDomains": domains,
        "deniedDomains": ["*"] if network_mode == "deny" else [],
        "strictAllowlist": True,
        **({"allowAllDomains": True} if network_mode == "all" else {}),
    }
    protected_paths: list[str] = []
    if sys.platform != "win32":
        protected_paths.extend(("~/.ssh", "~/.aws", "~/.config"))
        # Protect the service's local persistence and data directory even when
        # a generated skill guesses an absolute host path.
        database_bases = [
            Path.cwd() / "skill_agent_loop.db",
            Path(__file__).resolve().parents[2] / "skill_agent_loop.db",
        ]
        configured_path = sqlite_database_path(get_settings().database_url)
        if configured_path is not None:
            database_bases.append(configured_path)
        for base in database_bases:
            for suffix in ("", "-wal", "-shm", "-journal", ".bak"):
                protected_paths.append(str(base) + suffix)
        from app import paths

        data_root = paths.user_data_dir().resolve()
        protected_paths.append(str(data_root))
        protected_paths.extend(
            str(path)
            for path in (
                data_root / "logs",
                data_root / "network.json",
                data_root / "connector-locks",
            )
        )
    allow_read = [str(workspace.resolve())]
    runtime_root = _bundled_runtime_root()
    if runtime_root is not None:
        allow_read.append(str(runtime_root))
    allow_write = ["."]
    if sandbox_temp is not None:
        resolved_temp = str(sandbox_temp.resolve(strict=True))
        allow_read.append(resolved_temp)
        allow_write.append(resolved_temp)
    settings = json.dumps(
        {
            "filesystem": {
                "denyRead": protected_paths,
                "allowRead": allow_read,
                "allowWrite": allow_write,
                "denyWrite": [],
            },
            "network": network,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="staffdeck-srt-", suffix=".json", delete=False
    ) as handle:
        handle.write(settings)
        handle.flush()
    return Path(handle.name)


def _create_srt_temp_directory() -> tempfile.TemporaryDirectory[str]:
    try:
        # Keep the prefix short because SRT appends Unix-domain socket names;
        # Linux limits the complete socket path to roughly 108 bytes.
        return tempfile.TemporaryDirectory(prefix="sd-", ignore_cleanup_errors=True)
    except OSError as exc:
        raise HarnessExecutionError(
            "SANDBOX_TEMP_UNAVAILABLE",
            "SRT 无法创建临时运行目录，请检查主机临时目录权限和可用空间。",
            details={"exception_type": type(exc).__name__},
        ) from exc


def _srt_supports_allow_all() -> bool:
    resolved = resolve_srt()
    if resolved is None:
        return False
    marker = "staffdeck-allow-all-domains-patch-v1"
    manager_path = resolved[1].parent / "sandbox" / "sandbox-manager.js"
    config_path = resolved[1].parent / "sandbox" / "sandbox-config.js"
    try:
        return marker in manager_path.read_text(
            encoding="utf-8"
        ) and marker in config_path.read_text(encoding="utf-8")
    except OSError:
        return False


def _srt_argv(*, settings_path: Path, command: str) -> list[str]:
    resolved = resolve_srt()
    if resolved is None:
        raise HarnessExecutionError("SANDBOX_UNAVAILABLE", "SRT executable is unavailable.")
    node, cli = resolved
    if sys.platform == "win32":
        shell_args = ["-c", _windows_powershell_command(command)]
    else:
        shell_args = [_BASH_PATH, "--noprofile", "--norc", "-c", command]
    return [
        str(node),
        str(cli),
        "--settings",
        str(settings_path),
        *shell_args,
    ]


def _windows_powershell_command(command: str) -> str:
    # Windows PowerShell inherits the machine code page for redirected output;
    # force UTF-8 so the harness' UTF-8 decoder preserves non-ASCII results.
    runtime_alias = ""
    runtime = _bundled_runtime_root()
    if runtime is not None:
        python_path = str(runtime / "python.exe").replace("'", "''")
        runtime_alias = (
            f"$staffdeckPython = '{python_path}'\n"
            "function Invoke-StaffDeckPython { & $staffdeckPython @args }\n"
            "Set-Alias -Name python -Value Invoke-StaffDeckPython\n"
            "Set-Alias -Name python3 -Value Invoke-StaffDeckPython\n"
        )
    script = (
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "[Console]::OutputEncoding = $OutputEncoding\n"
        f"{runtime_alias}"
        f"{command}"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return f"{_WINDOWS_POWERSHELL} -NoProfile -NonInteractive -EncodedCommand {encoded}"


def _unsandboxed_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        encoded = _windows_powershell_command(command).split()[-1]
        return [_WINDOWS_POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    return [_BASH_PATH, "--noprofile", "--norc", "-c", command]


def _bundled_runtime_root() -> Path | None:
    """Return the frozen app's bundled Python directory, when present."""
    if sys.platform != "win32":
        return None
    root = Path(sys.executable).resolve().parent / "runtime"
    return root if (root / "python.exe").is_file() else None


def _bubblewrap_argv(
    *,
    sandbox_executable: str,
    workspace: Path,
    command: str,
    backend: str = "bubblewrap",
    env: dict[str, str] | None = None,
    extra_readonly_paths: Sequence[Path] = (),
    network_mode: str = "deny",
    sandbox_cwd: str = SANDBOX_WORKSPACE,
) -> list[str]:
    if network_mode not in {"all", "allowlist", "deny"}:
        raise HarnessExecutionError("SANDBOX_POLICY_INVALID", "Unknown sandbox network policy.")
    if backend == "seatbelt":
        if network_mode == "allowlist":
            raise HarnessExecutionError(
                "SANDBOX_POLICY_UNSUPPORTED",
                "macOS Seatbelt does not support domain allowlists; "
                "SRT is required for this policy.",
            )
        # Deny by default, keep the task workspace writable, and permit only
        # read access to the system runtime needed by basic shell commands.
        profile = (
            "(version 1) (deny default) "
            "(allow process*) (allow sysctl-read) "
            f'(allow file-read* (subpath "/usr")) '
            f'(allow file-read* (subpath "/bin")) '
            f'(allow file-read* (subpath "/private/etc")) '
            f'(allow file-read* (subpath "{workspace}")) '
            f'(allow file-write* (subpath "{workspace}")) '
            + ("(deny network*)" if network_mode == "deny" else "(allow network*)")
        )
        for raw_path in dict.fromkeys(str(path.resolve()) for path in extra_readonly_paths):
            path = Path(raw_path)
            if path.is_dir() and path != workspace and not path.is_relative_to(workspace):
                profile += f' (allow file-read* (subpath "{raw_path}"))'
        return [
            _seatbelt_executable(),
            "-p",
            profile,
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            command,
        ]
    argv = [
        sandbox_executable,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--cap-drop",
        "ALL",
        "--clearenv",
    ]
    if network_mode == "allowlist":
        raise HarnessExecutionError(
            "SANDBOX_POLICY_UNSUPPORTED",
            "Bubblewrap cannot enforce domain allowlists; SRT is required for this policy.",
        )
    if network_mode == "deny":
        argv.append("--unshare-net")
    for raw_path in _READ_ONLY_SYSTEM_PATHS:
        path = Path(raw_path)
        if path.is_symlink():
            argv.extend(("--symlink", os.readlink(path), raw_path))
        elif path.exists():
            argv.extend(("--ro-bind", raw_path, raw_path))
    for raw_path in dict.fromkeys(str(path.resolve()) for path in extra_readonly_paths):
        path = Path(raw_path)
        if path.is_dir() and path != workspace and not path.is_relative_to(workspace):
            argv.extend(("--ro-bind", raw_path, raw_path))
    argv.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--dir",
            SANDBOX_WORKSPACE,
            "--bind",
            str(workspace),
            SANDBOX_WORKSPACE,
            # Bubblewrap's root is otherwise an anonymous tmpfs. Remount only
            # that mount read-only; the nested workspace bind remains writable.
            "--remount-ro",
            "/",
            "--chdir",
            sandbox_cwd,
            "--setenv",
            "HOME",
            SANDBOX_WORKSPACE,
            "--setenv",
            "PWD",
            sandbox_cwd,
            "--setenv",
            "TMPDIR",
            SANDBOX_WORKSPACE,
            "--setenv",
            "PATH",
            _SANDBOX_PATH,
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "LC_ALL",
            "C.UTF-8",
        )
    )
    allowed_env = {
        key: value
        for key, value in (env or {}).items()
        if key in {
            "ARGUMENTS", "QUERY", "SKILL_WORKSPACE", "ARTIFACT_DIR", "SKILL_SLUG", "SKILL_NAME",
            "USER_ID", "SKILL_FILES_JSON", "SSL_CERT_FILE", "PIP_CERT",
        }
    }
    for key, value in allowed_env.items():
        if len(value) <= 16 * 1024:
            argv.extend(("--setenv", key, value))
    argv.extend(("--", _BASH_PATH, "--noprofile", "--norc", "-c", command))
    return argv


def _managed_process_environment(env: dict[str, str] | None) -> dict[str, str]:
    if sys.platform == "win32":
        system_keys = {
            "APPDATA",
            "ComSpec",
            "HOMEDRIVE",
            "HOMEPATH",
            "LOCALAPPDATA",
            "PATH",
            "PATHEXT",
            "PROGRAMDATA",
            "SystemRoot",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "WINDIR",
        }
        baseline = {key: os.environ[key] for key in system_keys if key in os.environ}
    else:
        baseline = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
    allowed = {
        key: value
        for key, value in (env or {}).items()
        if key in {
            "PATH", "HOME", "PWD", "TMPDIR", "LANG", "LC_ALL",
            "ARGUMENTS", "QUERY", "SKILL_WORKSPACE", "ARTIFACT_DIR", "SKILL_SLUG",
            "SKILL_NAME", "USER_ID", "SKILL_FILES_JSON", "SSL_CERT_FILE", "PIP_CERT",
        }
    }
    return {**baseline, **allowed}


def _run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    output_limit: int,
    stdin_bytes: bytes = b"",
    env: dict[str, str] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> _BoundedProcessResult:
    started = time.monotonic()
    try:
        managed_process = ManagedProcess.start(
            argv,
            cwd=str(cwd),
            env=_managed_process_environment(env),
            stdin=subprocess.PIPE if stdin_bytes else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except ManagedProcessError as exc:
        raise HarnessExecutionError("COMMAND_START_FAILED", str(exc)) from exc
    process = managed_process.process
    if process.stdout is None or process.stderr is None:
        managed_process.close()
        raise HarnessExecutionError(
            "COMMAND_START_FAILED",
            "Sandbox process did not expose bounded output streams.",
        )
    if stdin_bytes and process.stdin is not None:
        try:
            process.stdin.write(stdin_bytes)
            process.stdin.close()
        except OSError:
            process.stdin.close()

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
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if callable(is_cancelled) and is_cancelled():
                managed_process.close()
                raise HarnessExecutionError(
                    "SANDBOX_EXECUTION_CANCELLED",
                    "Sandbox execution was cancelled.",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                managed_process.close()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    raise HarnessExecutionError(
                        "COMMAND_TERMINATION_FAILED",
                        "Timed-out sandbox process could not be terminated.",
                    ) from exc
                break
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
    finally:
        managed_process.close()
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


def _validate_command(command: str) -> None:
    if not command or not command.strip():
        raise _command_denied("Command cannot be empty.")
    if "\x00" in command:
        raise _command_denied("Command cannot contain a null byte.")
    if "\r" in command:
        raise _command_denied("Carriage returns are not allowed in shell programs.")
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


def _validate_windows_command(command: str) -> None:
    if not command or not command.strip():
        raise _command_denied("Command cannot be empty.")
    if "\x00" in command:
        raise _command_denied("Command cannot contain a null byte.")
    if any(
        ord(character) < 0x20 and character not in {"\t", "\n", "\r"}
        for character in command
    ):
        raise _command_denied("Command contains an unsupported control character.")
    if len(command) > _MAX_COMMAND_CHARS:
        raise _command_denied("Command exceeds the maximum length.")
    if re.search(r"(?m)(?:^|\n)\s*<<\s*['\"]?", command):
        raise _command_denied(
            "Bash heredoc syntax is not supported on Windows PowerShell. "
            "Use write_file/General Skill for Python files, or a PowerShell here-string."
        )
    if "&&" in command:
        raise _command_denied(
            "Bash command chaining (&&) is not supported by this Windows PowerShell. "
            "Use newline-separated PowerShell statements or ';'."
        )
    if re.search(r"(?im)(?:^|[;&|\n])\s*py(?:\.exe)?(?:\s|$)", command):
        raise _command_denied(
            "py is not the bundled Python runtime on Windows. "
            "Use the General Skill Python runtime for Python execution."
        )
    if _WINDOWS_PROFILE_EXPANSION.search(command):
        raise _command_denied("Host profile path expansion is not allowed.")

def _validate_path_token(token: str) -> None:
    candidates = [token]
    if "=" in token:
        candidates.extend(part for part in token.split("=")[1:] if part)
    for candidate in candidates:
        normalized = candidate.replace("\\", "/")
        if ".harness-trash" in PurePosixPath(normalized).parts:
            raise _command_denied("Harness internal paths are not accessible.")


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
