"""Run UI assembly cases against the same isolated installed wheel."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
from uuid import uuid4

COMBINATIONS = {'minimal': {}, 'authentication': {'authentication': True},
    'catalog': {'catalog': True}, 'catalog_versions': {'catalog': True, 'versions': True},
    'concepts': {'concepts': True}, 'discovery': {'discovery': True},
    'concepts_discovery': {'concepts': True, 'discovery': True},
    'spaces_catalog_versions': {'spaces': True, 'catalog': True, 'versions': True}}


def summarize(root):
    checks = []
    for name in COMBINATIONS:
        folder = root / name
        files = [folder / 'report.json', folder / 'minimal-browser.json']
        try:
            service, browser = [json.loads(file.read_text()) for file in files]
            passed = service['status'] == browser['status'] == 'passed' and not browser.get('errors')
            status = 'passed' if passed else 'failed'
        except (OSError, ValueError, KeyError):
            status = 'not_executed'
        checks.append({'name': name, 'status': status,
            'evidence': [str(file.relative_to(root.parent)) for file in files]})
    return {'status': 'passed' if all(row['status'] == 'passed' for row in checks) else 'not_accepted', 'checks': checks}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed-python', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--playwright', required=True)
    parser.add_argument('--chrome', required=True)
    parser.add_argument('--combination', choices=tuple(COMBINATIONS))
    args = parser.parse_args()
    root = args.output_dir.resolve(); root.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith(('KM_', 'STAFFDECK_')) and key != 'PYTHONPATH'}
    for name, modules in COMBINATIONS.items():
        if args.combination and name != args.combination: continue
        output = root / name; output.mkdir(exist_ok=True)
        config = output / 'configuration.json'; config.write_text(json.dumps({'preset': 'minimal', 'modules': modules}))
        identity = output / 'identity.json'; identity.write_text(json.dumps({'test': {'id': 'test', 'spaces': ['*'], 'permissions': ['read', 'write']}}))
        with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        command = [str(args.installed_python.absolute()), '-m', 'knowledge_core.cli', '--config', str(config),
            '--database-url', 'sqlite:///' + str(output / ('knowledge-' + uuid4().hex + '.sqlite')), '--port', str(port)]
        if modules.get('authentication'): command += ['--identity-file', str(identity)]
        result = {'name': name, 'status': 'failed', 'command': command}
        with (output / 'service.log').open('w') as stream:
            service = subprocess.Popen(command, env=env, cwd=output, stdout=stream, stderr=subprocess.STDOUT)
            try:
                origin = f'http://127.0.0.1:{port}'
                for _ in range(100):
                    if service.poll() is not None: raise RuntimeError('Service exited before startup')
                    try:
                        with urllib.request.urlopen(origin + '/health/ready') as response:
                            if response.status == 200: break
                    except OSError: pass
                    time.sleep(.1)
                else: raise RuntimeError('Readiness timeout')
                browser_env = {**env, 'KM_ACCEPTANCE_URL': origin, 'KM_EVIDENCE_DIR': str(output),
                    'KM_PLAYWRIGHT_MODULE': args.playwright, 'PLAYWRIGHT_EXECUTABLE_PATH': args.chrome}
                with (output / 'browser.log').open('w') as browser_log:
                    subprocess.run([str(args.node), str(Path(__file__).with_name('minimal_browser.mjs'))], cwd=output,
                        env=browser_env, stdout=browser_log, stderr=subprocess.STDOUT, check=True)
                result['status'] = 'passed'
            except Exception as error: result['error'] = str(error)
            finally:
                service.terminate(); service.wait(timeout=30)
                result['shutdown_returncode'] = service.returncode
        (output / 'report.json').write_text(json.dumps(result, indent=2))
        print(name + ': ' + result['status'], flush=True)
    report = summarize(root)
    (root.parent / 'combination_browser.json').write_text(json.dumps(report, indent=2))
    raise SystemExit(0 if report['status'] == 'passed' else 1)


if __name__ == '__main__': main()
