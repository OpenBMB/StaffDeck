from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_supervisor_uses_platform_specific_executables() -> None:
    supervisor = _load_script("dev_supervisor")

    assert supervisor._backend_python("win32") == ROOT_DIR / "backend/.venv/Scripts/python.exe"
    assert supervisor._backend_python("linux") == ROOT_DIR / "backend/.venv/bin/python"
    assert supervisor._vite_executable("win32") == ROOT_DIR / "frontend-enterprise/node_modules/.bin/vite.cmd"
    assert supervisor._vite_executable("darwin") == ROOT_DIR / "frontend-enterprise/node_modules/.bin/vite"


def test_pid_alive_recognizes_current_process() -> None:
    process_utils = _load_script("process_utils")

    assert process_utils.pid_alive(os.getpid())


def test_dev_cli_uses_next_port_in_packaged_app_range(monkeypatch) -> None:
    dev = _load_script("dev")
    monkeypatch.delenv("ULTRARAG_PORT_RANGE_START", raising=False)
    monkeypatch.delenv("ULTRARAG_PORT_RANGE_END", raising=False)
    monkeypatch.setattr(dev, "_port_available", lambda _host, port: port != 5173)

    assert dev._select_available_port("127.0.0.1", 5173) == 5174


def test_dev_cli_honors_packaged_app_port_range(monkeypatch) -> None:
    dev = _load_script("dev")
    monkeypatch.setenv("ULTRARAG_PORT_RANGE_START", "6200")
    monkeypatch.setenv("ULTRARAG_PORT_RANGE_END", "6202")
    monkeypatch.setattr(dev, "_port_available", lambda _host, port: port == 6202)

    assert dev._select_available_port("127.0.0.1", 6200) == 6202


def test_supervisor_does_not_restart_during_startup_grace(monkeypatch) -> None:
    supervisor = _load_script("dev_supervisor")

    class RunningProcess:
        def poll(self):
            return None

    service = supervisor.Service(name="app", cwd=ROOT_DIR, command=["unused"])
    service.health_url = "http://127.0.0.1:5173/api/health"
    service.process = RunningProcess()
    service.startup_deadline = 100.0
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 50.0)
    monkeypatch.setattr(service, "healthy", lambda: False)

    service.poll()

    assert service.unhealthy_count == 0
    assert service.restart_count == 0


def test_shell_wrappers_delegate_to_cross_platform_cli() -> None:
    for command in ("up", "down", "status"):
        script = (SCRIPTS_DIR / f"dev_{command}.sh").read_text(encoding="utf-8")
        assert '$ROOT_DIR/backend/.venv/bin/python' in script
        assert 'scripts/dev.py" ' + command in script


def test_powershell_wrappers_delegate_to_cross_platform_cli() -> None:
    for command in ("up", "down", "status"):
        script = (SCRIPTS_DIR / f"dev_{command}.ps1").read_text(encoding="utf-8")
        assert f'"$PSScriptRoot\\dev.ps1" {command}' in script


def test_powershell_launcher_accepts_newer_python_3_versions() -> None:
    script = (SCRIPTS_DIR / "dev.ps1").read_text(encoding="utf-8")

    assert 'Prefix = @("-3.11")' in script
    assert 'Prefix = @("-3")' in script
