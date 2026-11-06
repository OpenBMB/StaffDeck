from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_url_ready_does_not_require_reading_response_body(monkeypatch) -> None:
    dev = _load_script("dev")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            raise ConnectionResetError("body connection closed")

    monkeypatch.setattr(dev.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert dev._url_ready("http://127.0.0.1:5173/api/health") is True


def test_url_ready_ignores_response_close_failure(monkeypatch) -> None:
    dev = _load_script("dev")

    class Response:
        status = 200

        def close(self):
            raise ConnectionResetError("connection closed")

    monkeypatch.setattr(dev.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert dev._url_ready("http://127.0.0.1:5173/api/health") is True


def test_dev_cli_honors_packaged_app_port_range(monkeypatch) -> None:
    dev = _load_script("dev")
    monkeypatch.setenv("ULTRARAG_PORT_RANGE_START", "6200")
    monkeypatch.setenv("ULTRARAG_PORT_RANGE_END", "6202")
    monkeypatch.setattr(dev, "_port_available", lambda _host, port: port == 6202)

    assert dev._select_available_port("127.0.0.1", 6200) == 6202


def test_dev_cli_keeps_complete_frontend_dependencies(monkeypatch) -> None:
    dev = _load_script("dev")
    calls: list[list[str]] = []
    monkeypatch.setattr(dev, "_npm_executable", lambda: "npm")

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(dev.subprocess, "run", run)

    dev._ensure_frontend_dependencies()

    assert len(calls) == 1
    assert calls[0][-3:] == ["ls", "--depth=0", "--json"]


def test_dev_cli_refreshes_incomplete_frontend_dependencies(monkeypatch) -> None:
    dev = _load_script("dev")
    calls: list[list[str]] = []
    monkeypatch.setattr(dev, "_npm_executable", lambda: "npm")

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if "ls" in command else 0)

    monkeypatch.setattr(dev.subprocess, "run", run)

    dev._ensure_frontend_dependencies()

    assert len(calls) == 2
    assert calls[1][-3:] == ["ci", "--no-audit", "--no-fund"]


def test_dev_cli_detects_missing_backend_dependency(tmp_path, monkeypatch) -> None:
    dev = _load_script("dev")
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text(
        '[project]\ndependencies = ["present>=1", "missing>=1"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(dev, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(
        dev,
        "_installed_distribution_version",
        lambda name: "1.2" if name == "present" else None,
    )

    assert dev._backend_dependencies_complete() is False


def test_dev_cli_refreshes_incomplete_backend_dependencies(monkeypatch) -> None:
    dev = _load_script("dev")
    calls: list[list[str]] = []
    monkeypatch.setattr(dev, "_backend_dependencies_complete", lambda: False)

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(dev.subprocess, "run", run)

    dev._ensure_backend_dependencies()

    assert calls == [
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-e",
            str(ROOT_DIR / "backend"),
        ]
    ]


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


@pytest.fixture
def oss_runtime_checkout(tmp_path, monkeypatch):
    dev = _load_script('dev')
    (tmp_path / 'distribution').mkdir()
    (tmp_path / 'distribution/manifest.json').write_text('{}')
    monkeypatch.setattr(dev, 'ROOT_DIR', tmp_path)
    monkeypatch.delenv('HARNESS_V3_ROOT', raising=False)
    monkeypatch.delenv('HARNESS_V3_NODE_BIN', raising=False)
    entry = tmp_path / 'harness-engine/apps/cli/lib/bin.js'
    entry.parent.mkdir(parents=True)
    sdk = {'available': True}
    original = dev.importlib.util.find_spec
    monkeypatch.setattr(dev.importlib.util, 'find_spec',
                        lambda name, *args: (object() if sdk['available'] else None)
                        if name == 'deepseek_harness' else original(name, *args))
    return dev, tmp_path, entry, sdk


@pytest.mark.parametrize('missing', ['entry', 'dependencies', 'sdk'])
def test_dev_cli_prepares_incomplete_oss_runtime(oss_runtime_checkout, monkeypatch, missing):
    dev, root, entry, sdk = oss_runtime_checkout
    if missing != 'entry':
        entry.write_text('// built')
    if missing != 'dependencies':
        (root / 'harness-engine/node_modules').mkdir()
    sdk['available'] = missing != 'sdk'
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs == {'cwd': root, 'check': True}
        entry.write_text('// rebuilt')
        (root / 'harness-engine/node_modules').mkdir(exist_ok=True)
        sdk['available'] = True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(dev.subprocess, 'run', run)
    assert dev._ensure_oss_runtime() is True
    assert calls == [
        [sys.executable, str(root / 'prepare.py'), '--without-backend', '--no-cli-link'],
        [sys.executable, str(root / 'packaging/distribution/install_dependencies.py')],
    ]
    assert dev._ensure_oss_runtime() is False
    assert len(calls) == 2


def test_dev_cli_ready_runtime_uses_bundled_node_without_rebuilding(
    oss_runtime_checkout, monkeypatch,
):
    dev, root, entry, _sdk = oss_runtime_checkout
    entry.write_text('// built')
    (root / 'harness-engine/node_modules').mkdir()
    node = root / 'packaging/sandbox_runtime/bin' / (
        'node.exe' if sys.platform == 'win32' else 'node'
    )
    node.parent.mkdir(parents=True)
    node.write_text('bundled node')
    monkeypatch.setenv('PATH', '/existing/path')
    monkeypatch.setattr(dev.subprocess, 'run', lambda *_args, **_kw: pytest.fail('rebuilt runtime'))
    assert dev._ensure_oss_runtime() is False
    assert os.environ['PATH'].split(os.pathsep)[0] == str(node.parent)
    assert os.environ['HARNESS_V3_NODE_BIN'] == str(node)
    monkeypatch.setenv('HARNESS_V3_NODE_BIN', '/custom/node')
    assert dev._ensure_oss_runtime() is False
    assert os.environ['HARNESS_V3_NODE_BIN'] == '/custom/node'


def test_dev_cli_keeps_external_runtime_and_legacy_checkout(oss_runtime_checkout, monkeypatch):
    dev, root, _entry, _sdk = oss_runtime_checkout
    monkeypatch.setenv('HARNESS_V3_ROOT', str(root / 'external-engine'))
    monkeypatch.setattr(dev.subprocess, 'run', lambda *_args, **_kw: pytest.fail('prepared external'))
    assert dev._ensure_oss_runtime() is False
    (root / 'distribution/manifest.json').unlink()
    monkeypatch.delenv('HARNESS_V3_ROOT')
    assert dev._ensure_oss_runtime() is False


def test_dev_cli_preparation_failure_keeps_running_services(oss_runtime_checkout, monkeypatch):
    dev, _root, _entry, _sdk = oss_runtime_checkout
    calls = []
    monkeypatch.setattr(dev, '_ensure_backend_dependencies', lambda: calls.append('backend'))
    monkeypatch.setattr(dev, '_ensure_sandbox_runtime', lambda: calls.append('sandbox'))
    monkeypatch.setattr(dev, 'stop_services', lambda **_kw: pytest.fail('stopped old service'))
    monkeypatch.setattr(dev, '_load_supervisor', lambda: pytest.fail('continued after failure'))

    def fail(command, **_kwargs):
        calls.append('prepare')
        raise subprocess.CalledProcessError(13, command)

    monkeypatch.setattr(dev.subprocess, 'run', fail)
    assert dev.main(['up', '--detach']) == 1
    assert calls == ['backend', 'sandbox', 'prepare']


def test_dev_cli_rejects_success_without_runtime_output(oss_runtime_checkout, monkeypatch):
    dev, _root, _entry, _sdk = oss_runtime_checkout
    monkeypatch.setattr(dev.subprocess, 'run',
                        lambda command, **_kw: subprocess.CompletedProcess(command, 0))
    with pytest.raises(RuntimeError, match='did not produce a complete Harness'):
        dev._ensure_oss_runtime()


@pytest.mark.parametrize('prepared', [False, True])
def test_dev_cli_finishes_all_preparation_before_stopping_services(monkeypatch, prepared):
    dev = _load_script('dev')
    calls = []
    for name, label in [('_ensure_backend_dependencies', 'backend'),
                        ('_ensure_sandbox_runtime', 'sandbox'),
                        ('_ensure_frontend_dependencies', 'frontend-dependencies'),
                        ('_build_frontend', 'frontend-build')]:
        monkeypatch.setattr(dev, name, lambda label=label: calls.append(label))
    monkeypatch.setattr(dev, '_ensure_oss_runtime', lambda: calls.append('prepare') or prepared)
    supervisor = SimpleNamespace(SINGLE_PORT=True, APP_HOST='127.0.0.1', APP_PORT='5173',
                                 validate_prerequisites=lambda: calls.append('validate'),
                                 main=lambda: calls.append('launch') or 0)
    monkeypatch.setattr(dev, '_load_supervisor', lambda: supervisor)
    monkeypatch.setattr(dev, 'stop_services', lambda **_kw: calls.append('stop'))
    monkeypatch.setattr(dev, '_select_available_port', lambda *_args: 5173)
    monkeypatch.setattr(dev, '_ensure_port_available', lambda *_args: None)
    assert dev.command_up(False) == 0
    expected = ['backend', 'sandbox', 'prepare']
    if not prepared:
        expected += ['frontend-dependencies']
    expected += ['validate']
    if not prepared:
        expected += ['frontend-build']
    assert calls == [*expected, 'stop', 'launch']


def test_dev_cli_frontend_build_failure_keeps_running_services(monkeypatch):
    dev = _load_script('dev')
    for name in ('_ensure_backend_dependencies', '_ensure_sandbox_runtime',
                 '_ensure_frontend_dependencies'):
        monkeypatch.setattr(dev, name, lambda: None)
    monkeypatch.setattr(dev, '_ensure_oss_runtime', lambda: False)
    monkeypatch.setattr(dev, '_load_supervisor', lambda: SimpleNamespace(
        SINGLE_PORT=True, validate_prerequisites=lambda: None,
    ))
    monkeypatch.setattr(dev, 'stop_services', lambda **_kw: pytest.fail('stopped old service'))
    monkeypatch.setattr(dev, '_build_frontend',
                        lambda: (_ for _ in ()).throw(RuntimeError('frontend build failed')))
    assert dev.main(['up']) == 1


def test_powershell_launcher_prefers_existing_backend_environment():
    script = (SCRIPTS_DIR / 'dev.ps1').read_text(encoding='utf-8')
    assert script.index('backend\\.venv\\Scripts\\python.exe') < script.index('$env:PYTHON')
