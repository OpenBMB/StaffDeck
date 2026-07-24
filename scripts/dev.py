#!/usr/bin/env python3
"""Cross-platform development lifecycle commands for StaffDeck."""

from __future__ import annotations

import argparse
import importlib
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


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    names = ("npm.cmd", "npm") if sys.platform == "win32" else ("npm",)
    for name in names:
        executable = shutil.which(name)
        if executable:
            return executable
    raise RuntimeError("npm is not available on PATH; install Node.js 20 or newer")


def _build_frontend() -> None:
    print("Building frontend bundle for single-port app...")
    subprocess.run(
        [_npm_executable(), "--prefix", str(ROOT_DIR / "frontend-enterprise"), "run", "build"],
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
    supervisor.validate_prerequisites()
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
