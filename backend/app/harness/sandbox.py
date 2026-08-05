from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from app.harness.errors import HarnessExecutionError


@dataclass(frozen=True)
class SandboxDiagnostics:
    status: str
    code: str | None
    message: str
    remediation: str | None = None
    backend: str | None = None


SandboxNetworkMode = Literal["all", "allowlist", "deny"]


def parse_network_policy(value: object | None) -> SandboxNetworkMode:
    if value is None or str(value).strip() == "":
        return "all"
    normalized = str(value).strip()
    if normalized in {"all", "allowlist", "deny"}:
        return cast(SandboxNetworkMode, normalized)
    raise HarnessExecutionError(
        "SANDBOX_POLICY_INVALID",
        "租户沙盒网络策略无效，已拒绝执行。",
    )


def diagnostics() -> SandboxDiagnostics:
    """Explain whether this process can safely start the configured sandbox."""
    backend = available_backend()
    if backend is None:
        return SandboxDiagnostics(
            "unavailable", "SANDBOX_UNAVAILABLE",
            "未检测到可用的 SRT 或 Bubblewrap 沙盒运行时。",
            "请修复安装或重新安装 StaffDeck。",
        )
    return _environment_diagnostics(backend)


def _environment_diagnostics(backend: str) -> SandboxDiagnostics:
    if sys.platform.startswith("linux"):
        if backend == "srt" and os.geteuid() == 0:
            return SandboxDiagnostics(
                "unavailable", "SANDBOX_ROOT_USER",
                "当前 StaffDeck 由 root 用户运行，Linux SRT 无法安全创建 UID 映射。",
                "请使用独立的普通服务账号（例如 staffdeck）重启 StaffDeck。",
            )
        userns_clone = _read_int("/proc/sys/kernel/unprivileged_userns_clone")
        max_namespaces = _read_int("/proc/sys/user/max_user_namespaces")
        if userns_clone == 0 or max_namespaces == 0:
            return SandboxDiagnostics(
                "unavailable", "SANDBOX_USERNS_DISABLED",
                "当前主机禁止 Linux user namespace，SRT 无法启动。",
                "请启用 kernel.unprivileged_userns_clone=1，"
                "并将 user.max_user_namespaces 设置为大于 0。",
            )
    if sys.platform == "win32" and backend == "srt":
        resolved = resolve_srt()
        if resolved is None or not _windows_srt_ready(*resolved):
            return SandboxDiagnostics(
                "unavailable",
                "SANDBOX_WINDOWS_SETUP_REQUIRED",
                "Windows 安全执行环境尚未完成初始化，或账户/WFP 网络规则不可用。",
                f"请在这台 Windows 主机上确认 UAC 后执行：{windows_install_command()}",
                backend=backend,
            )
    return SandboxDiagnostics("ready", None, f"沙盒可用（{backend}）。", backend=backend)


def windows_install_command() -> str:
    resolved = resolve_srt()
    if resolved is None:
        return "重新运行 StaffDeck 安装程序以修复沙盒组件"
    node, cli = resolved
    return subprocess.list2cmdline([str(node), str(cli), "windows-install"])


@lru_cache(maxsize=4)
def _windows_srt_ready(node: Path, cli: Path) -> bool:
    """Run SRT initialization once; upstream verifies user credentials and WFP."""
    try:
        with tempfile.TemporaryDirectory(prefix="staffdeck-srt-probe-") as raw_dir:
            settings = Path(raw_dir) / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "filesystem": {"denyRead": [], "allowWrite": ["."], "denyWrite": []},
                        "network": {
                            "allowedDomains": [],
                            "deniedDomains": ["*"],
                            "strictAllowlist": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    str(node), str(cli), "--settings", str(settings),
                    "-c", "exit 0",
                ],
                # The sandbox account cannot enter the real user's private temp
                # directory. The installed bundle is readable by local Users.
                cwd=node.parent,
                capture_output=True,
                timeout=20,
                check=False,
            )
            return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_backend_usable(backend: str) -> None:
    report = _environment_diagnostics(backend)
    if report.status != "ready":
        detail = report.message + (f" {report.remediation}" if report.remediation else "")
        raise HarnessExecutionError(report.code or "SANDBOX_UNAVAILABLE", detail)


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def available_backend() -> str | None:
    """Return the strongest local process sandbox available on this host."""
    if resolve_srt() is not None:
        return "srt"
    if sys.platform.startswith("linux") and _trusted_executable("bwrap"):
        return "bubblewrap"
    return None


def resolve_srt() -> tuple[Path, Path] | None:
    """Resolve ``(node, cli.js)`` from a reviewed bundle or the host PATH."""
    candidates: list[tuple[Path, Path]] = []
    explicit = os.environ.get("STAFFDECK_SRT_RUNTIME", "").strip()
    if explicit:
        root = Path(explicit).expanduser()
        candidates.append(
            (
                root / "bin" / _node_name(),
                root
                / "node_modules"
                / "@anthropic-ai"
                / "sandbox-runtime"
                / "dist"
                / "cli.js",
            )
        )
    # Source deployments use the same patched, lockfile-backed bundle as
    # packaged applications. It must win over an arbitrary global npm shim.
    source_root = _source_runtime_root()
    candidates.append(
        (
            source_root / "bin" / _node_name(),
            source_root
            / "node_modules"
            / "@anthropic-ai"
            / "sandbox-runtime"
            / "dist"
            / "cli.js",
        )
    )
    try:
        executable = Path(sys.executable).resolve()
        roots = [executable.parent / "sandbox"]
        if sys.platform == "darwin" and executable.parent.name == "MacOS":
            roots.insert(0, executable.parent.parent / "Resources" / "sandbox")
        for root in roots:
            candidates.append(
                (
                    root / "bin" / _node_name(),
                    root
                    / "node_modules"
                    / "@anthropic-ai"
                    / "sandbox-runtime"
                    / "dist"
                    / "cli.js",
                )
            )
    except OSError:
        pass
    allow_global = os.environ.get("STAFFDECK_ALLOW_GLOBAL_SRT", "").strip().lower()
    srt = shutil.which("srt") if allow_global in {"1", "true", "yes", "on"} else None
    node = shutil.which("node") if srt else None
    if srt and node:
        # npm's bin entry is a shim/symlink; resolving it yields the package's
        # actual dist/cli.js entrypoint.
        candidates.append((Path(node), Path(srt).resolve()))
    for node_path, cli_path in candidates:
        if node_path.is_file() and cli_path.is_file() and os.access(node_path, os.X_OK):
            return node_path.resolve(), cli_path.resolve()
    return None


def _node_name() -> str:
    return "node.exe" if sys.platform == "win32" else "node"


def _source_runtime_root() -> Path:
    return Path(__file__).resolve().parents[3] / "packaging" / "sandbox_runtime"


def require_backend() -> str:
    report = diagnostics()
    if report.status != "ready":
        detail = report.message + (f" {report.remediation}" if report.remediation else "")
        raise HarnessExecutionError(report.code or "SANDBOX_UNAVAILABLE", detail)
    backend = available_backend()
    assert backend is not None
    return backend


def _trusted_executable(name: str) -> bool:
    executable = shutil.which(name)
    if not executable:
        return False
    path = Path(executable)
    try:
        return path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


__all__ = [
    "SandboxDiagnostics", "available_backend", "diagnostics", "ensure_backend_usable",
    "parse_network_policy", "require_backend", "resolve_srt", "windows_install_command",
]
