import argparse
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve(); output.mkdir(parents=True, exist_ok=True)
    model = json.loads(args.model_config.read_text())
    report = {'status': 'not_accepted', 'model': model['model'], 'adapter': 'OpenAI-compatible direct', 'fallback_allowed': False}
    env = {key: value for key, value in os.environ.items() if not key.startswith(('KM_', 'STAFFDECK_')) and key != 'PYTHONPATH'}
    with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    with (output / 'service.log').open('w') as log:
        service = subprocess.Popen([str(args.python.absolute()), '-m', 'knowledge_core.cli', '--database-url', 'sqlite:///' + str(output / 'direct.sqlite'),
            '--model-config', str(args.model_config.resolve()), '--port', str(port)], cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=180) as client:
                for _ in range(100):
                    if service.poll() is not None: raise RuntimeError('KM exited before startup')
                    try:
                        if client.get('/health/ready').is_success: break
                    except httpx.HTTPError: pass
                    time.sleep(.1)
                response = client.post('/api/km/v1/documents', json={'filename': 'temporary-model-expense.md',
                    'content_base64': base64.b64encode(b'# Expense policy\n\nExpense reimbursement requires original receipts within 30 days.\n\n## Approval\n\nManagers approve expense claims.').decode()})
                response.raise_for_status(); job_id = response.json()['id']; report['job_id'] = job_id
                for _ in range(400):
                    job = client.get('/api/km/v1/jobs/' + job_id).json()
                    if job['status'] in {'failed', 'succeeded'}: break
                    time.sleep(.5)
                report['task'] = job
                if job['status'] != 'succeeded': raise ValueError('Real ingestion did not succeed')
                document = client.get('/api/km/v1/documents/' + job['document_id']).json()
                trace = document['metadata']['route_trace']; report['ingest_trace'] = trace
                if not any(row['phase'] == 'bucket_model' for row in trace) or any('fallback' in row['phase'] for row in trace):
                    raise ValueError('Real ingestion model fell back')
                response = client.post('/api/km/v1/query', json={'query': 'What receipts are required for expense reimbursement?', 'use_model': True})
                response.raise_for_status(); result = response.json(); report['query'] = result
                if not result['chunks'] or not any(row['phase'] == 'document_route_model' for row in result['route_trace']) or any('fallback' in row['phase'] for row in result['route_trace']):
                    raise ValueError('Real query model did not produce valid evidence without fallback')
                report['status'] = 'passed'
        except Exception as error:
            report['error'] = str(error)
            raise
        finally:
            service.terminate(); service.wait(timeout=30)
            report['shutdown_returncode'] = service.returncode
            (output / 'direct-model.json').write_text(json.dumps(report, indent=2))


if __name__ == '__main__': main()
