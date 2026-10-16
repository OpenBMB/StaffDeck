"""Exercise the persistent source service without touching any host database."""
import argparse
import base64
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve(); output.mkdir(parents=True, exist_ok=True)
    service = Path(__file__).with_name('source_service.py')
    directory = Path.home() / '.local/share/knowledge-core/km-1013'
    report = {'status': 'not_accepted', 'commands': [], 'requests': []}
    def command(*arguments, success=True):
        result = subprocess.run([sys.executable, str(service), *arguments], capture_output=True, text=True)
        report['commands'].append({'arguments': arguments, 'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        assert (result.returncode == 0) == success, result.stderr
        return result
    try:
        status = json.loads(command('status').stdout)
        assert status['running'] and status['ready']
        command('start', success=False)
        with tempfile.TemporaryDirectory(prefix='km-port-conflict-') as temporary, socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0)); occupied.listen()
            command('start', '--directory', temporary, '--port', str(occupied.getsockname()[1]), success=False)
            assert occupied.fileno() >= 0
        with httpx.Client(base_url='http://127.0.0.1:18890', timeout=180) as client:
            def request(method, path, **kwargs):
                response = client.request(method, '/api/km/v1' + path, **kwargs)
                response.raise_for_status(); value = response.json()
                report['requests'].append({'method': method, 'path': path, 'request': kwargs, 'status': response.status_code, 'response': value})
                return value
            config = client.get('/api/km/config').json()
            assert not config['identity_required'] and not config['modules']['publication']
            space = request('POST', '/spaces', json={'id': 'deployment-' + str(time.time_ns()), 'name': 'Deployment persistence'})['id']
            base = request('POST', '/knowledge-bases', json={'space_id': space, 'name': 'Deployment persistence'})['id']
            scope = {'space_id': space, 'knowledge_base_id': base}
            content = '# Persistence\n\nPERSIST731 original receipts survive a normal restart.'
            job = request('POST', '/documents', json={**scope, 'filename': 'persistent.md', 'content_base64': base64.b64encode(content.encode()).decode()})
            for _ in range(600):
                task = request('GET', '/jobs/' + job['id'], params=scope)
                if task['status'] in {'succeeded', 'failed', 'cancelled'}: break
                time.sleep(.1)
            assert task['status'] == 'succeeded', task
            before = request('POST', '/query', json={**scope, 'query': 'PERSIST731', 'use_model': False})
            assert before['chunks'] and not any('model' in row['phase'] for row in before['route_trace'])
            chunk = before['chunks'][0]; citation = chunk.get('citation_id', chunk['id'])
            command('restart')
            after = request('POST', '/query', json={**scope, 'query': 'PERSIST731', 'use_model': False})
            assert after['chunks'][0]['id'] == chunk['id']
            old_source = request('GET', '/citations/' + citation, params=scope)
            assert 'PERSIST731' in old_source['content']
            assert json.loads(command('status').stdout)['ready']
        with sqlite3.connect(directory / 'knowledge.sqlite') as db:
            report['tables'] = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            assert all(name.startswith('km_') or name == 'sqlite_sequence' for name in report['tables'])
        inspection = subprocess.run([str(directory / 'venv/bin/python'), '-I', '-c',
            'import importlib.util,json,knowledge_core; print(json.dumps({"core":knowledge_core.__file__,"app":importlib.util.find_spec("app") is not None,"staffdeck_harness":importlib.util.find_spec("staffdeck_harness") is not None}))'],
            cwd=directory, capture_output=True, text=True, check=True)
        report['imports'] = json.loads(inspection.stdout)
        assert not report['imports']['app'] and not report['imports']['staffdeck_harness']
        report['status'] = 'passed'
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        (output / 'report.json').write_text(json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'status': report['status'], 'error': report.get('error')}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
