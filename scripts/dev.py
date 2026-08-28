#!/usr/bin/env python3
"""Cross-platform development lifecycle commands for StaffDeck."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from process_utils import pid_alive

ROOT_DIR = Path(__file__).resolve().parent.parent
RUN_DIR = ROOT_DIR / ".dev"
LOG_DIR = RUN_DIR / "logs"
SERVICE_NAMES = ("supervisor", "app", "backend", "enterprise", "chat")
DEFAULT_PORT_RANGE_START = 5173
DEFAULT_PORT_RANGE_END = 5199


def _configured_env(name: str) -> str | None:
    """Read one non-secret setting without importing the application.

    The lifecycle command runs from the repository root while the backend
    reads ``backend/.env`` from its own working directory.  Reading only the
    structured-PDF settings here keeps the preflight check aligned with the
    backend without loading or printing model credentials.
    """
    value = os.environ.get(name)
    if value is not None:
        return value.strip()

    dotenv_path = os.environ.get("ULTRARAG_DOTENV")
    path = Path(dotenv_path).expanduser() if dotenv_path else ROOT_DIR / "backend" / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key.strip() != name:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip()
    return None


def _env_flag(name: str, default: bool = False) -> bool:
    raw = _configured_env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _structured_pdf_model_dir() -> Path:
    configured = _configured_env("RAPID_MODELS_DIR")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return ROOT_DIR / "backend" / "models" / "rapiddoc"


def _structured_pdf_state() -> dict[str, object]:
    """Return local RapidDoc readiness without importing or downloading OCR."""
    enabled = _env_flag("STRUCTURED_PDF_ENABLED")
    engine = _configured_env("STRUCTURED_PDF_ENGINE") or "rapiddoc"
    state: dict[str, object] = {
        "enabled": enabled,
        "engine": engine,
        "ready": False,
        "manifest_exists": False,
        "missing_count": 0,
        "version": None,
    }
    if not enabled:
        state["status"] = "disabled"
        return state

    model_dir = _structured_pdf_model_dir()
    manifest_path = model_dir / "manifest.json"
    state["manifest_exists"] = manifest_path.is_file()
    if not model_dir.is_dir():
        state["missing_count"] = 1
        state["status"] = "needs_prepare"
        return state
    if not manifest_path.is_file():
        state["missing_count"] = 1
        state["status"] = "needs_prepare"
        return state

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state["missing_count"] = 1
        state["status"] = "needs_prepare"
        state["error"] = "invalid_manifest"
        return state

    if not isinstance(manifest, dict):
        state["missing_count"] = 1
        state["status"] = "needs_prepare"
        state["error"] = "invalid_manifest"
        return state
    state["version"] = manifest.get("version")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        state["missing_count"] = 1
        state["status"] = "needs_prepare"
        state["error"] = "manifest_files_missing"
        return state
    missing_count = sum(1 for item in files if not (model_dir / str(item)).is_file())
    state["missing_count"] = missing_count
    state["ready"] = missing_count == 0
    state["status"] = "ready" if state["ready"] else "needs_prepare"
    return state


def _ensure_structured_pdf_readiness() -> dict[str, object]:
    state = _structured_pdf_state()
    if not state["enabled"] or state["ready"]:
        return state
    if sys.platform == "win32":
        command = r".\backend\.venv\Scripts\python.exe scripts\prepare_rapiddoc_models.py --prepare"
    else:
        command = "backend/.venv/bin/python scripts/prepare_rapiddoc_models.py --prepare"
    raise RuntimeError(
        "STRUCTURED_PDF_ENABLED is true, but the offline RapidDoc model is not ready "
        f"(missing artifacts: {state['missing_count']}). Run {command} explicitly, "
        "then start StaffDeck again. No model download was attempted."
    )


def _pid_file(name: str) -> Path:
    return RUN_DIR / f"{name}.pid"


def _app_port_file() -> Path:
    return RUN_DIR / "app.port"


def _read_pid(name: str) -> int | None:
    try:
        raw = _pid_file(name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _terminate_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    for _ in range(30):
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def stop_services(verbose: bool = True) -> None:
    for name in SERVICE_NAMES:
        pid = _read_pid(name)
        _pid_file(name).unlink(missing_ok=True)
        if pid is None:
            continue
        if pid_alive(pid):
            _terminate_pid(pid)
            if verbose:
                print(f"Stopped {name} ({pid})")
        elif verbose:
            print(f"Removed stale {name} pid ({pid})")
    _app_port_file().unlink(missing_ok=True)


def _listening_pids(port: int) -> list[int]:
    if sys.platform == "win32":
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            capture_output=True,
            check=False,
        )
        pids: set[int] = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
                continue
            if fields[1].rsplit(":", 1)[-1] == str(port) and fields[4].isdigit():
                pids.add(int(fields[4]))
        return sorted(pids)
    lsof = shutil.which("lsof")
    if not lsof:
        return []
    result = subprocess.run(
        [lsof, "-tiTCP:" + str(port), "-sTCP:LISTEN"],
        text=True,
        capture_output=True,
        check=False,
    )
    return sorted({int(line) for line in result.stdout.splitlines() if line.isdigit()})


def _port_available(host: str, port: int) -> bool:
    bind_host = "0.0.0.0" if host == "0.0.0.0" else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            # 与 uvicorn 监听行为保持一致:不设 REUSEADDR 时,刚停止的进程
            # 留下的 TIME_WAIT 连接会让 bind 失败,被误判为端口被占而漂移。
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, port))
            return True
        except OSError:
            return False


def _ensure_port_available(host: str, port: int, force: bool) -> None:
    if _port_available(host, port):
        return
    pids = _listening_pids(port)
    if force and pids:
        for pid in pids:
            _terminate_pid(pid)
        time.sleep(0.3)
        if _port_available(host, port):
            return
    details = ", ".join(str(pid) for pid in pids) or "unknown process"
    raise RuntimeError(
        f"Port {port} is already in use by {details}. "
        "Run the down command first or set FORCE_PORTS=1."
    )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _port_candidates(preferred: int) -> list[int]:
    start = _env_int("ULTRARAG_PORT_RANGE_START", DEFAULT_PORT_RANGE_START)
    end = _env_int("ULTRARAG_PORT_RANGE_END", DEFAULT_PORT_RANGE_END)
    if start > end:
        start, end = end, start
    return [preferred] + [port for port in range(start, end + 1) if port != preferred]


def _select_available_port(host: str, preferred: int) -> int:
    candidates = _port_candidates(preferred)
    for port in candidates:
        if _port_available(host, port):
            return port
    raise RuntimeError(f"No available StaffDeck port in {candidates[0]}-{candidates[-1]}")


def _restore_runtime_port() -> None:
    if "APP_PORT" in os.environ:
        return
    try:
        value = _app_port_file().read_text(encoding="utf-8").strip()
    except OSError:
        return
    if value.isdigit():
        os.environ["APP_PORT"] = value


def _npm_executable() -> str:
    configured = os.environ.get("STAFFDECK_NPM", "").strip()
    if configured:
        executable = Path(configured).expanduser()
        if executable.is_file():
            return str(executable)
        raise RuntimeError(f"STAFFDECK_NPM does not point to a file: {configured}")

    names = ("npm.cmd", "npm") if sys.platform == "win32" else ("npm",)
    for name in names:
        executable = shutil.which(name)
        if executable:
            return executable
    raise RuntimeError("npm is not available on PATH; install Node.js 20 or newer")


def _npm_environment() -> dict[str, str]:
    environment = os.environ.copy()
    configured_node = os.environ.get("STAFFDECK_NODE", "").strip()
    if not configured_node:
        return environment

    node = Path(configured_node).expanduser()
    if not node.is_file():
        raise RuntimeError(f"STAFFDECK_NODE does not point to a file: {configured_node}")
    node_directory = str(node.resolve().parent)
    path_entries = environment.get("PATH", "").split(os.pathsep)
    if node_directory not in path_entries:
        environment["PATH"] = os.pathsep.join([node_directory, *path_entries])
    return environment


def _build_frontend() -> None:
    print("Building frontend bundle for single-port app...")
    subprocess.run(
        [_npm_executable(), "--prefix", str(ROOT_DIR / "frontend-enterprise"), "run", "build"],
        cwd=ROOT_DIR,
        env=_npm_environment(),
        check=True,
    )


def _ensure_frontend_dependencies() -> None:
    """Refresh node_modules after a pull adds or changes direct dependencies."""
    frontend_dir = ROOT_DIR / "frontend-enterprise"
    npm = _npm_executable()
    dependency_check = subprocess.run(
        [npm, "--prefix", str(frontend_dir), "ls", "--depth=0", "--json"],
        cwd=ROOT_DIR,
        env=_npm_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if dependency_check.returncode == 0:
        return
    print("Frontend dependencies changed or are incomplete; running npm ci...")
    subprocess.run(
        [npm, "--prefix", str(frontend_dir), "ci", "--no-audit", "--no-fund"],
        cwd=ROOT_DIR,
        env=_npm_environment(),
        check=True,
    )


def _ensure_sandbox_runtime() -> None:
    runtime = ROOT_DIR / "packaging" / "sandbox_runtime"
    cli = runtime / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist" / "cli.js"
    node = runtime / "bin" / ("node.exe" if sys.platform == "win32" else "node")
    manager = cli.parent / "sandbox" / "sandbox-manager.js"
    marker = "staffdeck-allow-all-domains-patch-v1"
    try:
        if node.is_file() and cli.is_file() and marker in manager.read_text(encoding="utf-8"):
            return
    except OSError:
        pass
    print("Preparing the reviewed StaffDeck sandbox runtime...")
    subprocess.run(
        [
            sys.executable,
            str(ROOT_DIR / "packaging" / "fetch_sandbox_runtime.py"),
            str(runtime),
        ],
        cwd=ROOT_DIR,
        check=True,
    )


def _url_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            response.read()
            return response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def _wait_for_url(label: str, url: str, log_file: Path) -> None:
    deadline = time.monotonic() + _env_int("DEV_STARTUP_TIMEOUT", 180)
    while time.monotonic() < deadline:
        if _url_ready(url):
            return
        time.sleep(0.5)
    print(f"{label} failed to become ready: {url}", file=sys.stderr)
    if log_file.exists():
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        print("\n".join(lines), file=sys.stderr)
    raise RuntimeError(f"{label} did not become ready")


def _load_supervisor():
    import dev_supervisor

    return dev_supervisor


def _service_ports(supervisor) -> list[tuple[str, int]]:
    if supervisor.SINGLE_PORT:
        return [(supervisor.APP_HOST, int(supervisor.APP_PORT))]
    return [
        (supervisor.BACKEND_HOST, int(supervisor.BACKEND_PORT)),
        (supervisor.ENTERPRISE_HOST, int(supervisor.ENTERPRISE_PORT)),
    ]


def _start_detached(supervisor) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout = (LOG_DIR / "supervisor.log").open("ab", buffering=0)
    stderr = (LOG_DIR / "supervisor.err.log").open("ab", buffering=0)
    options: dict[str, object] = {"start_new_session": True}
    if sys.platform == "win32":
        options = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        }
    process = subprocess.Popen(
        [sys.executable, str(ROOT_DIR / "scripts" / "dev_supervisor.py")],
        cwd=ROOT_DIR,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        **options,
    )
    return process.pid


def command_up(detach_flag: bool) -> int:
    detach = detach_flag or _env_flag("DETACH")
    os.environ.setdefault("AUTO_RESTART", "1" if detach else "0")
    supervisor = _load_supervisor()
    _ensure_structured_pdf_readiness()
    _ensure_frontend_dependencies()
    supervisor.validate_prerequisites()
    _ensure_sandbox_runtime()
    stop_services(verbose=False)
    force_ports = _env_flag("FORCE_PORTS")
    if supervisor.SINGLE_PORT and not force_ports:
        preferred_port = int(supervisor.APP_PORT)
        selected_port = _select_available_port(supervisor.APP_HOST, preferred_port)
        if selected_port != preferred_port:
            print(f"Port {preferred_port} is in use; using {selected_port} instead.")
            os.environ["APP_PORT"] = str(selected_port)
            supervisor = importlib.reload(supervisor)
    for host, port in _service_ports(supervisor):
        _ensure_port_available(host, port, force_ports)
    if supervisor.SINGLE_PORT:
        _build_frontend()

    if not detach:
        print("StaffDeck development services are starting. Press Ctrl-C to stop.")
        return supervisor.main()

    pid = _start_detached(supervisor)
    services = supervisor.build_services()
    for service in services:
        if service.health_url:
            _wait_for_url(service.name, service.health_url, service.log_file)
    if supervisor.SINGLE_PORT:
        base = f"http://{supervisor.url_host(supervisor.APP_HOST)}:{supervisor.APP_PORT}"
        _wait_for_url("chat", base + "/chat/", LOG_DIR / "app.log")
        _wait_for_url("enterprise", base + "/enterprise/dashboard", LOG_DIR / "app.log")
        print(f"Started StaffDeck supervisor ({pid})")
        print(f"  app        {base}/chat/")
        print(f"  enterprise {base}/enterprise/dashboard")
        print(f"  api docs   {base}/docs")
    else:
        backend = f"http://{supervisor.url_host(supervisor.BACKEND_HOST)}:{supervisor.BACKEND_PORT}"
        frontend = f"http://{supervisor.url_host(supervisor.ENTERPRISE_HOST)}:{supervisor.ENTERPRISE_PORT}"
        print(f"Started StaffDeck supervisor ({pid})")
        print(f"  backend    {backend}/docs")
        print(f"  enterprise {frontend}/enterprise/dashboard")
        print(f"  chat       {frontend}/chat/")
    print(f"Logs: {LOG_DIR}")
    return 0


def command_status() -> int:
    _restore_runtime_port()
    supervisor = _load_supervisor()
    names = ("supervisor", "app") if supervisor.SINGLE_PORT else ("supervisor", "backend", "enterprise")
    print("Processes:")
    for name in names:
        pid = _read_pid(name)
        state = f"running ({pid})" if pid and pid_alive(pid) else "not running"
        print(f"  {name:<10} {state}")
    print("Ports:")
    for host, port in _service_ports(supervisor):
        state = "available" if _port_available(host, port) else "listening"
        print(f"  {port:<10} {state}")
    print("Health:")
    for service in supervisor.build_services():
        if service.health_url:
            state = "ok" if _url_ready(service.health_url) else "unavailable"
            print(f"  {service.name:<10} {state} ({service.health_url})")
    structured_pdf = _structured_pdf_state()
    print("Structured PDF:")
    print(f"  enabled    {structured_pdf['enabled']}")
    print(f"  engine     {structured_pdf['engine']}")
    print(f"  readiness  {structured_pdf['status']}")
    print(f"  manifest   {'present' if structured_pdf['manifest_exists'] else 'missing'}")
    print(f"  missing    {structured_pdf['missing_count']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    up_parser = subparsers.add_parser("up", help="build and start development services")
    up_parser.add_argument("--detach", action="store_true", help="run under the background supervisor")
    subparsers.add_parser("down", help="stop development services")
    subparsers.add_parser("status", help="show process, port, and health status")
    args = parser.parse_args(argv)
    try:
        if args.command == "up":
            return command_up(args.detach)
        if args.command == "down":
            stop_services()
            return 0
        return command_status()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
