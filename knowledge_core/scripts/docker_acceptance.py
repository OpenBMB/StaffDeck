"""Build the self-contained directory and exercise its container UI."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--playwright', required=True)
    parser.add_argument('--chrome', required=True)
    args = parser.parse_args()
    root = args.output_dir.resolve(); root.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1]
    image = 'knowledge-core:modular-acceptance'
    container = 'km-modular-' + uuid4().hex[:12]
    build = ['docker', 'build', '--progress=plain', '--build-arg', 'HTTP_PROXY=', '--build-arg', 'HTTPS_PROXY=',
             '--build-arg', 'http_proxy=', '--build-arg', 'https_proxy=', '--build-arg', 'NO_PROXY=*', '-t', image, '.']
    result = {'status': 'failed', 'checks': [], 'build_command': build, 'build_source': str(source)}
    started = False
    try:
        with (root / 'build.log').open('w') as log:
            subprocess.run(build, cwd=source, stdout=log, stderr=subprocess.STDOUT, check=True)
        result['checks'].append({'name': 'self_contained_build', 'status': 'passed', 'evidence': ['docker/build.log']})
        with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        command = ['docker', 'run', '--detach', '--name', container, '-p', f'127.0.0.1:{port}:8787', image]
        result['run_command'] = command
        subprocess.run(command, capture_output=True, text=True, check=True); started = True
        origin = f'http://127.0.0.1:{port}'
        for _ in range(100):
            try:
                with urllib.request.urlopen(origin + '/health/ready') as response:
                    if response.status == 200: break
            except OSError: pass
            time.sleep(.1)
        else: raise RuntimeError('Container readiness timeout')
        env = {**os.environ, 'KM_ACCEPTANCE_URL': origin, 'KM_EVIDENCE_DIR': str(root),
               'KM_PLAYWRIGHT_MODULE': args.playwright, 'PLAYWRIGHT_EXECUTABLE_PATH': args.chrome,
               'KM_REQUIRE_REAL_MODEL': '0', 'NODE_OPTIONS': ''}
        with (root / 'browser.log').open('w') as log:
            subprocess.run([str(args.node), str(source / 'scripts/minimal_browser.mjs')], env=env,
                           cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True)
        storage = subprocess.run(['docker', 'exec', container, 'python', '-c',
            "import json,sqlite3,importlib.util; c=sqlite3.connect('/data/knowledge.sqlite'); "
            "print(json.dumps({'tables':[r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")],"
            "'documents':c.execute('SELECT count(*) FROM km_documents').fetchone()[0],"
            "'legacy_modules':{name:importlib.util.find_spec('knowledge_core.'+name) is not None for name in "
            "['service','full_api','full_runtime','okf','versions','publication']}}))"],
            capture_output=True, text=True, check=True)
        data = json.loads(storage.stdout)
        expected = {'km_documents', 'km_buckets', 'km_chunks', 'km_ingest_jobs', 'km_citation_snapshots', 'km_schema_versions'}
        if set(data['tables']) != expected or not data['documents'] or any(data['legacy_modules'].values()): raise RuntimeError('Unexpected container storage or retired runtime')
        (root / 'storage.json').write_text(json.dumps(data, indent=2))
        result['checks'].append({'name': 'container_workflow_and_storage', 'status': 'passed',
            'evidence': ['docker/minimal-browser.json', 'docker/storage.json', 'docker/minimal-desktop.png', 'docker/minimal-mobile.png']})
        result['status'] = 'passed'
    except Exception as error: result['error'] = str(error)
    finally:
        if started:
            subprocess.run(['docker', 'stop', '--time', '30', container], capture_output=True, check=True)
            state = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', container], capture_output=True, text=True, check=True)
            shutdown = json.loads(state.stdout)
            (root / 'shutdown.json').write_text(json.dumps(shutdown, indent=2))
            logs = subprocess.run(['docker', 'logs', container], capture_output=True, text=True, check=True)
            (root / 'service.log').write_text(logs.stdout + logs.stderr)
            if shutdown['Running'] or shutdown['OOMKilled'] or 'Application shutdown complete' not in logs.stderr + logs.stdout:
                result['status'] = 'failed'; result['error'] = 'Container did not shut down normally'
            else:
                result['checks'].append({'name': 'normal_shutdown', 'status': 'passed', 'evidence': ['docker/shutdown.json', 'docker/service.log']})
            subprocess.run(['docker', 'rm', container], capture_output=True, check=True)
    (root.parent / 'docker.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    raise SystemExit(0 if result['status'] == 'passed' else 1)


if __name__ == '__main__': main()
