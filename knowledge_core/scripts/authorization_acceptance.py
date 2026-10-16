"""Exercise real authenticated HTTP transport in a disposable KM instance."""
import argparse
import base64
import json
import os
from pathlib import Path
import socket
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
    source = Path(__file__).resolve().parents[1]
    report = {'status': 'not_accepted', 'cases': []}
    with tempfile.TemporaryDirectory(prefix='km-authorization-') as temporary:
        root = Path(temporary)
        config = root / 'configuration.json'
        config.write_text(json.dumps({'preset': 'full', 'modules': {'authentication': True, 'publication': False}}))
        identity = root / 'identity.json'
        identity.write_text(json.dumps({
            'owner': {'id': 'owner', 'tenant_id': 'test', 'spaces': ['*'], 'permissions': ['read', 'write', 'history:write']},
            'writer': {'id': 'writer', 'tenant_id': 'test', 'spaces': ['a'], 'permissions': ['read', 'write']},
            'reader': {'id': 'reader', 'tenant_id': 'test', 'spaces': ['a'], 'permissions': ['read']},
            'outside': {'id': 'outside', 'tenant_id': 'test', 'spaces': ['b'], 'permissions': ['read', 'write']},
        }))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        env = {key: value for key, value in os.environ.items() if not key.startswith(('KM_', 'STAFFDECK_')) and key != 'PYTHONPATH'}
        env['PYTHONPATH'] = str(source / 'src')
        with (output / 'service.log').open('w') as log:
            child = subprocess.Popen([sys.executable, '-m', 'knowledge_core.cli', '--config', str(config), '--identity-file', str(identity), '--database-url', 'sqlite:///' + str(root / 'knowledge.sqlite'), '--port', str(port)], env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=30) as client:
                    for _ in range(300):
                        try:
                            if client.get('/health/ready').is_success: break
                        except httpx.HTTPError: pass
                        time.sleep(.1)
                    def request(name, actor, method, path, expected, **kwargs):
                        headers = {'Authorization': 'Bearer ' + actor} if actor else {}
                        response = client.request(method, '/api/km/v1' + path, headers=headers, **kwargs)
                        value = response.json()
                        report['cases'].append({'name': name, 'actor': actor, 'method': method, 'path': path, 'request': kwargs, 'expected_status': expected, 'status': response.status_code, 'response': value})
                        assert response.status_code == expected, (name, response.text)
                        return value
                    request('unauthenticated', None, 'GET', '/identity', 401)
                    for space in ('a', 'b'):
                        request('create_scope_' + space, 'owner', 'POST', '/spaces', 201, json={'id': space, 'name': space})
                    base = request('create_base', 'writer', 'POST', '/knowledge-bases', 201, json={'space_id': 'a', 'name': 'Permissions'})['id']
                    scope = {'space_id': 'a', 'knowledge_base_id': base}
                    body = {**scope, 'filename': 'auth.md', 'content_base64': base64.b64encode(b'# Authorization\n\nAUTH731 receipt policy.').decode()}
                    request('reader_write_denied', 'reader', 'POST', '/documents', 403, json=body)
                    request('cross_space_denied', 'outside', 'GET', '/documents', 403, params=scope)
                    request('actor_override_denied', 'writer', 'POST', '/documents', 422, json={**body, 'actor': 'owner'})
                    task = request('writer_ingestion', 'writer', 'POST', '/documents', 202, json=body)
                    for _ in range(300):
                        job = request('trusted_task_read', 'writer', 'GET', '/jobs/' + task['id'], 200, params=scope)
                        if job['status'] in {'succeeded', 'failed', 'cancelled'}: break
                        time.sleep(.1)
                    assert job['status'] == 'succeeded'
                    request('cross_space_job_denied', 'outside', 'GET', '/jobs/' + task['id'], 403, params=scope)
                    path = '/documents/' + job['document_id']
                    doc = request('reader_read', 'reader', 'GET', path, 200, params=scope)
                    request('reader_edit_denied', 'reader', 'PUT', path, 403, params=scope, json={'expected_updated_at': doc['updated_at'], 'content_md': 'forbidden'})
                    request('missing_edit_revision', 'writer', 'PUT', path, 422, params=scope, json={'content_md': 'missing revision'})
                    request('stale_edit_revision', 'writer', 'PUT', path, 409, params=scope, json={'expected_updated_at': 'stale', 'content_md': 'stale'})
                    request('stale_delete_revision', 'writer', 'DELETE', path, 409, params={**scope, 'expected_updated_at': 'stale'})
                    report['status'] = 'passed'
            except Exception as error:
                report['error'] = type(error).__name__ + ': ' + str(error)
            finally:
                child.terminate(); child.wait(timeout=30)
                report['shutdown_returncode'] = child.returncode
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'error': report.get('error')}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
