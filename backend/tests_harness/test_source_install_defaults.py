import importlib.util
import json
import os
from pathlib import Path

from staffdeck_harness.distribution import bootstrap

ROOT = Path(__file__).parents[2]


def installer():
    spec = importlib.util.spec_from_file_location('distribution_prepare', ROOT / 'packaging/distribution/prepare.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dual_lock_frontend_uses_npm(tmp_path):
    (tmp_path / 'package-lock.json').write_text('{}')
    (tmp_path / 'pnpm-lock.yaml').write_text("lockfileVersion: '6.0'")
    assert installer().package_manager(tmp_path) == ['npm']


def test_oss_install_does_not_build_business_ui(tmp_path, monkeypatch):
    module = installer()
    (tmp_path / 'distribution').mkdir()
    (tmp_path / 'distribution/manifest.json').write_text(json.dumps({
        'installation': {'tenant_id': 'oss-test'}, 'assembly': {'ui_variant': 'releases'},
    }))
    for name in ('frontend-enterprise', 'frontend-business'):
        (tmp_path / name / 'dist').mkdir(parents=True)
        (tmp_path / name / 'package.json').write_text('{}')
    (tmp_path / 'harness-engine/apps/cli/lib').mkdir(parents=True)
    (tmp_path / 'harness-engine/apps/cli/lib/bin.js').write_text('')
    calls = []
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'run', lambda args, cwd=None, env=None: calls.append((args, cwd)))
    module.prepare(backend=False)
    assert any(cwd == tmp_path / 'frontend-enterprise' for _, cwd in calls)
    assert not any(cwd == tmp_path / 'frontend-business' for _, cwd in calls)


def test_source_database_is_in_installation_data_directory(tmp_path, monkeypatch):
    for key in ('DATABASE_URL', 'APP_SECRET', 'ULTRARAG_DOTENV', 'HARNESS_RUNTIME_CONFIG_PATH', 'HARNESS_V3_ROOT', 'HARNESS_V3_HOME'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ULTRARAG_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(bootstrap, 'load_manifest', lambda: {
        'assembly': {}, 'installation': {'control_auth_provider': ''}, 'deployment': {'services': []},
    })
    bootstrap.configure_environment()
    assert os.environ['DATABASE_URL'] == f'sqlite:///{tmp_path / "skill_agent_loop.db"}'
    secret = os.environ['APP_SECRET']
    bootstrap.configure_environment()
    assert os.environ['APP_SECRET'] == secret
    assert (tmp_path / '.installation-seed').stat().st_mode & 0o777 == 0o600


def test_windows_package_manager_shims_execute_through_node_without_a_shell(tmp_path,monkeypatch):
    module=installer()
    node=tmp_path/'node/node.exe';node.parent.mkdir()
    shim=tmp_path/'global/npm.cmd';shim.parent.mkdir()
    script=shim.parent/'node_modules/npm/bin/npm-cli.js'
    script.parent.mkdir(parents=True);script.write_text('npm entry fixture')
    monkeypatch.setattr(module.shutil,'which',lambda name:str(node if name=='node' else shim))
    monkeypatch.setattr(module.subprocess,'check_output',lambda *a,**kw:str(node)+'\n')
    assert module.windows_command(['npm','ci'])==[str(node),str(script),'ci']
