"""Build only the copied KM directory, install its wheel and run browser acceptance."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from zipfile import ZipFile
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--uv', type=Path, required=True)
    parser.add_argument('--playwright', required=True)
    parser.add_argument('--chrome', required=True)
    parser.add_argument('--model-config', type=Path)
    args = parser.parse_args()
    output = args.output_dir.resolve(); output.mkdir(parents=True, exist_ok=True)
    report_path = output / 'isolated-artifact.json'
    report_path.write_text(json.dumps({'status': 'running'}))
    source = Path(__file__).resolve().parents[1]
    copied = output / 'isolated-source'
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns('node_modules', 'acceptance', 'dist', '__pycache__', '*.egg-info'), dirs_exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'KM_CONFIG', 'KM_DATABASE_URL', 'KM_CAPABILITIES_URL', 'KM_PUBLISHER_URL'}}
    env.update(PATH=str(args.node.parent) + ':' + str(args.uv.parent) + ':/usr/bin:/bin', NODE_OPTIONS='', UV=str(args.uv))
    commands = []
    def run(command, log, cwd=copied):
        commands.append({'command': command, 'cwd': str(cwd), 'log': log})
        with (output / log).open('w') as stream:
            subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True)
    run([str(args.node), 'build.mjs'], 'build.log')
    if not (output / 'venv/bin/python').exists(): run([str(args.uv), 'venv', str(output / 'venv')], 'venv.log')
    python = output / 'venv/bin/python'
    wheel = next((copied / 'dist').glob('*.whl'))
    run([str(args.uv), 'pip', 'install', '--reinstall', '--python', str(python), str(wheel)], 'install.log')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    database = output / ('minimal-' + uuid4().hex + '.sqlite')
    audit = output / 'runtime-imports.json'
    child = '''import json,sys
from pathlib import Path
from contextlib import asynccontextmanager
audit_path = Path(sys.argv[1])
def audit():
    audit_path.write_text(json.dumps(sorted(name for name in sys.modules if name.startswith(('knowledge_core.', 'app.', 'staffdeck.', 'staffdeck_harness.', 'harness.'))), indent=2))
import knowledge_core.cli as cli
create = cli.create_app
def audited_app(*args, **kwargs):
    app = create(*args, **kwargs)
    original = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        try:
            async with original(app):
                yield
        finally:
            audit()
    app.router.lifespan_context = lifespan
    return app
cli.create_app = audited_app
sys.argv = [sys.argv[0], *sys.argv[2:]]
cli.main()
'''
    with (output / 'service.log').open('w') as log:
        service = subprocess.Popen([str(python), '-c', child, str(audit), '--database-url', 'sqlite:///' + str(database), '--port', str(port),
            *(['--model-config', str(args.model_config.resolve())] if args.model_config else [])], cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if service.poll() is not None: raise RuntimeError('Installed KM exited before readiness')
                try:
                    with urllib.request.urlopen(url + '/health/ready') as response:
                        if response.status == 200: break
                except OSError: pass
                time.sleep(.1)
            else: raise RuntimeError('KM not ready')
            env.update(KM_ACCEPTANCE_URL=url, KM_EVIDENCE_DIR=str(output), KM_PLAYWRIGHT_MODULE=args.playwright,
                       PLAYWRIGHT_EXECUTABLE_PATH=args.chrome, KM_REQUIRE_REAL_MODEL='1' if args.model_config else '0')
            run([str(args.node), str(copied / 'scripts/minimal_browser.mjs')], 'browser.log', output)
        except Exception as error:
            report_path.write_text(json.dumps({'status': 'failed', 'error': str(error), 'commands': commands}))
            raise
        finally:
            service.terminate()
            service.wait(timeout=30)
    loaded = json.loads(audit.read_text())
    forbidden = ('app.', 'staffdeck.', 'staffdeck_harness.', 'harness.', 'knowledge_core.modules.',
                 'knowledge_core.service', 'knowledge_core.models', 'knowledge_core.database', 'knowledge_core.migration', 'knowledge_core.full_')
    if any(name.startswith(forbidden) for name in loaded):
        raise RuntimeError('Minimal service loaded disabled or host implementation')
    with ZipFile(wheel) as archive:
        files = archive.namelist()
        if any('knowledge_core/' + name + '.py' in files for name in ('service', 'full_runtime', 'full_api', 'okf', 'versions', 'publication')):
            raise RuntimeError('Product wheel contains retired runtime implementation')
    report = {'status': 'passed', 'source': str(copied), 'wheel': str(wheel), 'commands': commands,
              'database': str(database), 'shutdown_returncode': service.returncode, 'host_source_on_pythonpath': False,
              'runtime_imports': 'runtime-imports.json', 'wheel_legacy_runtime_absent': True}
    report_path.write_text(json.dumps(report, indent=2))


if __name__ == '__main__': main()
