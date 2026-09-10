#!/usr/bin/env python3
"""Local development supervisor for StaffDeck services."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from process_utils import pid_alive


ROOT_DIR = Path(__file__).resolve().parent.parent
RUN_DIR = ROOT_DIR / ".dev"
LOG_DIR = RUN_DIR / "logs"


def env_value(name: str, default: str) -> str:
    return os.environ.get(name) or default


SINGLE_PORT = env_value("SINGLE_PORT", "1") != "0"
APP_HOST = env_value("APP_HOST", "127.0.0.1")
APP_PORT = env_value("APP_PORT", "5173")
BACKEND_HOST = env_value("BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = env_value("BACKEND_PORT", "8000")
ENTERPRISE_HOST = env_value("ENTERPRISE_HOST", "127.0.0.1")
ENTERPRISE_PORT = env_value("ENTERPRISE_PORT", "5173")
AUTO_RESTART = env_value("AUTO_RESTART", "1") != "0"
STARTUP_GRACE_SECONDS = float(env_value("DEV_STARTUP_TIMEOUT", "180"))

api_host = "127.0.0.1" if BACKEND_HOST == "0.0.0.0" else BACKEND_HOST
API_BASE_URL = env_value("VITE_API_BASE_URL", env_value("API_BASE_URL", "" if SINGLE_PORT else f"http://{api_host}:{BACKEND_PORT}"))
TOOL_BASE_URL = env_value("TOOL_BASE_URL", f"http://localhost:{APP_PORT if SINGLE_PORT else BACKEND_PORT}")

if SINGLE_PORT:
    default_cors_origins = ",".join(
        [
            f"http://localhost:{APP_PORT}",
            f"http://127.0.0.1:{APP_PORT}",
        ]
    )
else:
    default_cors_origins = ",".join(
        [
            f"http://localhost:{ENTERPRISE_PORT}",
            f"http://127.0.0.1:{ENTERPRISE_PORT}",
        ]
    )
CORS_ORIGINS = env_value("CORS_ORIGINS", default_cors_origins)


def url_host(host: str) -> str:
    return "127.0.0.1" if host == "0.0.0.0" else host


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with (LOG_DIR / "supervisor.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")
    if sys.stdout.isatty():
        print(line, flush=True)


def remove_pid_file(path: Path, expected_pid: int | None) -> None:
    if expected_pid is None:
        return
    try:
        current = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    if current == str(expected_pid):
        path.unlink(missing_ok=True)


def remove_port_file(path: Path, expected_port: str) -> None:
    try:
        current = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    if current == expected_port:
        path.unlink(missing_ok=True)


@dataclass
class Service:
    name: str
    cwd: Path
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    health_url: str | None = None
    process: subprocess.Popen[bytes] | None = None
    unhealthy_count: int = 0
    restart_count: int = 0
    startup_deadline: float = 0.0
    has_been_healthy: bool = False

    @property
    def pid_file(self) -> Path:
        return RUN_DIR / f"{self.name}.pid"

    @property
    def log_file(self) -> Path:
        return LOG_DIR / f"{self.name}.log"

    @property
    def err_file(self) -> Path:
        return LOG_DIR / f"{self.name}.err.log"

    def start(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        merged_env = os.environ.copy()
        merged_env.update(self.env)
        stdout = self.log_file.open("ab", buffering=0)
        stderr = self.err_file.open("ab", buffering=0)
        process_options: dict[str, object] = {"start_new_session": True}
        if sys.platform == "win32":
            process_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=merged_env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            **process_options,
        )
        self.pid_file.write_text(f"{self.process.pid}\n", encoding="utf-8")
        self.unhealthy_count = 0
        self.startup_deadline = time.monotonic() + STARTUP_GRACE_SECONDS
        self.has_been_healthy = False
        log(f"started {self.name} pid={self.process.pid}")

    def stop(self) -> None:
        pid = self.current_pid()
        if pid is None:
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            remove_pid_file(self.pid_file, pid)
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            remove_pid_file(self.pid_file, pid)
            return
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for _ in range(30):
            if not self.pid_alive(pid):
                remove_pid_file(self.pid_file, pid)
                return
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        remove_pid_file(self.pid_file, pid)

    def current_pid(self) -> int | None:
        if self.process and self.process.poll() is None:
            return self.process.pid
        if not self.pid_file.exists():
            return None
        value = self.pid_file.read_text(encoding="utf-8").strip()
        if not value.isdigit():
            return None
        pid = int(value)
        return pid if self.pid_alive(pid) else None

    @staticmethod
    def pid_alive(pid: int) -> bool:
        return pid_alive(pid)

    def poll(self) -> None:
        if self.process is None:
            self.start()
            return
        exit_code = self.process.poll()
        if exit_code is not None:
            if not AUTO_RESTART:
                raise RuntimeError(f"{self.name} exited with code {exit_code}")
            self.restart_count += 1
            log(f"{self.name} exited code={exit_code}; restarting count={self.restart_count}")
            remove_pid_file(self.pid_file, self.process.pid)
            time.sleep(min(10, self.restart_count))
            self.start()
            return
        if self.health_url:
            if self.healthy():
                self.has_been_healthy = True
                self.unhealthy_count = 0
                return
            if not self.has_been_healthy and time.monotonic() < self.startup_deadline:
                return
            self.unhealthy_count += 1
            log(f"{self.name} health check failed count={self.unhealthy_count}")
            if self.unhealthy_count >= 8:
                log(f"{self.name} unhealthy too long; restarting")
                self.stop()
                self.start()

    def healthy(self) -> bool:
        if not self.health_url:
            return True
        response = None
        try:
            response = urllib.request.urlopen(self.health_url, timeout=2)
            return 200 <= response.status < 500
        except Exception:
            return False
        finally:
            if response is not None:
                try:
                    close = getattr(response, "close", None)
                    if close:
                        close()
                except OSError:
                    pass


def build_services() -> list[Service]:
    backend_python = _backend_python()
    if SINGLE_PORT:
        return [
            Service(
                name="app",
                cwd=ROOT_DIR / "backend",
                command=[
                    str(backend_python),
                    "-m",
                    "uvicorn",
                    "single_port_app:app",
                    "--host",
                    APP_HOST,
                    "--port",
                    APP_PORT,
                ],
                env={"CORS_ORIGINS": CORS_ORIGINS, "TOOL_BASE_URL": TOOL_BASE_URL},
                health_url=f"http://{url_host(APP_HOST)}:{APP_PORT}/api/health",
            )
        ]

    return [
        Service(
            name="backend",
            cwd=ROOT_DIR / "backend",
            command=[
                str(backend_python),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                BACKEND_HOST,
                "--port",
                BACKEND_PORT,
            ],
            env={"CORS_ORIGINS": CORS_ORIGINS, "TOOL_BASE_URL": TOOL_BASE_URL},
            health_url=f"http://{url_host(BACKEND_HOST)}:{BACKEND_PORT}/api/health",
        ),
        Service(
            name="enterprise",
            cwd=ROOT_DIR / "frontend-enterprise",
            command=[
                str(_vite_executable()),
                "--host",
                ENTERPRISE_HOST,
                "--port",
                ENTERPRISE_PORT,
                "--strictPort",
            ],
            env={"VITE_API_BASE_URL": API_BASE_URL},
            health_url=f"http://{url_host(ENTERPRISE_HOST)}:{ENTERPRISE_PORT}/enterprise/dashboard",
        ),
    ]


def _backend_python(platform: str | None = None) -> Path:
    platform = platform or sys.platform
    relative = Path("Scripts/python.exe") if platform == "win32" else Path("bin/python")
    return ROOT_DIR / "backend" / ".venv" / relative


def _vite_executable(platform: str | None = None) -> Path:
    platform = platform or sys.platform
    name = "vite.cmd" if platform == "win32" else "vite"
    return ROOT_DIR / "frontend-enterprise" / "node_modules" / ".bin" / name


def validate_prerequisites() -> None:
    missing = [path for path in (_backend_python(), _vite_executable()) if not path.exists()]
    if missing:
        formatted = "\n".join(f"  - {path}" for path in missing)
        raise RuntimeError(f"Development dependencies are missing:\n{formatted}")
    if shutil.which("node") is None:
        raise RuntimeError("Node.js is not available on PATH")


def main() -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    supervisor_pid = os.getpid()
    supervisor_pid_file = RUN_DIR / "supervisor.pid"
    app_port_file = RUN_DIR / "app.port"
    supervisor_pid_file.write_text(f"{supervisor_pid}\n", encoding="utf-8")
    if SINGLE_PORT:
        app_port_file.write_text(f"{APP_PORT}\n", encoding="utf-8")
    validate_prerequisites()
    services = build_services()
    stopping = False

    def handle_signal(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        log(f"received signal {signum}; stopping services")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_signal)

    log("supervisor started")
    try:
        for service in services:
            service.start()
        while not stopping:
            for service in services:
                service.poll()
            time.sleep(2)
    finally:
        for service in services:
            service.stop()
        remove_pid_file(supervisor_pid_file, supervisor_pid)
        if SINGLE_PORT:
            remove_port_file(app_port_file, APP_PORT)
        log("supervisor stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
