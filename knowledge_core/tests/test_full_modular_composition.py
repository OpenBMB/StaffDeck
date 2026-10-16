import base64
import time
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from knowledge_core.assembly import assemble
from knowledge_core.configuration import Configuration
from knowledge_core.runtime import Runtime
from knowledge_core.api import create_app
from knowledge_core.kernel import storage as s


def test_full_projection_uses_owned_modules_for_real_content(tmp_path):
    runtime = Runtime('sqlite:///' + str(tmp_path / 'full.sqlite'), configuration=Configuration('full'))
    app = create_app(runtime)
    with TestClient(app) as client:
        assert not any(name.startswith('knowledge_') for name in inspect(runtime.engine).get_table_names())
        assert client.get('/api/km/v1/spaces').json()[0]['id'] == 'local'
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Policies'})
        assert base.status_code == 201, base.text
        base_id = base.json()['id']
        assert client.post('/api/km/v1/knowledge-bases', json={'name': 'Policies'}).status_code == 409
        task = client.post('/api/km/v1/knowledge-bases/' + base_id + '/documents', json={
            'filename': 'receipts.md', 'content_base64': base64.b64encode(b'# Expenses\n\nExpense receipts are required.').decode()})
        assert task.status_code == 202, task.text
        for _ in range(100):
            job = client.get('/api/km/v1/jobs/' + task.json()['id'])
            assert job.status_code == 200, job.text
            if job.json()['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        assert job.json()['status'] == 'succeeded', job.text
        document_id = job.json()['document_id']
        document = client.get('/api/km/v1/documents/' + document_id)
        assert document.status_code == 200, document.text
        assert document.json()['knowledge_base_id'] == base_id
        result = client.post('/api/km/v1/query', json={'query': 'Expense receipts'})
        assert result.status_code == 200 and result.json()['chunks'], result.text
        citation = result.json()['chunks'][0]['id']
        assert client.get('/api/km/v1/citations/' + citation).status_code == 200
        assert client.get('/api/km/v1/knowledge-bases/' + base_id + '/okf').content.startswith(b'PK')
        copied = client.post('/api/km/v1/knowledge-bases/' + base_id + '/versions/copy', json={})
        assert copied.status_code == 200, copied.text
        assert copied.json()['head_version_id'] != document.json()['knowledge_base_version_id']
        assert client.get('/api/km/v1/documents/' + document_id).status_code == 200
        assert client.get('/api/km/v1/citations/' + citation).status_code == 200
        assert client.post('/api/km/v1/knowledge-bases/' + base_id + '/versions/publish', json={}).status_code == 200


def test_full_restore_materializes_history_retained_only_in_snapshots(tmp_path):
    from knowledge_core.conversion import convert
    from sqlalchemy import select
    path = tmp_path / 'history.sqlite'
    configuration = Configuration('full')
    runtime = Runtime('sqlite:///' + str(path), configuration=configuration)
    with TestClient(create_app(runtime)) as client:
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'History'}).json()['id']
        task = client.post(f'/api/km/v1/knowledge-bases/{base}/documents', json={
            'filename': 'original.md', 'content_base64': base64.b64encode(b'Historical expense receipts').decode()}).json()
        for _ in range(200):
            job = client.get('/api/km/v1/jobs/' + task['id']).json()
            if job['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        assert job['status'] == 'succeeded'
        original = client.get('/api/km/v1/documents/' + job['document_id']).json()
        version = original['knowledge_base_version_id']
        old_chunk = client.get('/api/km/v1/documents/' + original['id'] + '/chunks').json()[0]['id']
        assert client.post(f'/api/km/v1/knowledge-bases/{base}/versions/copy', json={}).status_code == 200
    assert convert(path, configuration, tmp_path / 'backups')['ok']
    runtime = Runtime('sqlite:///' + str(path), configuration=configuration)
    with TestClient(create_app(runtime)) as client:
        with runtime.engine.connect() as db:
            assert not db.execute(select(s.documents.c.id).where(s.documents.c.id == original['id'])).first()
        response = client.post(f'/api/km/v1/knowledge-bases/{base}/versions/rollback', json={'version_id': version})
        assert response.status_code == 200, response.text
        docs = client.get(f'/api/km/v1/knowledge-bases/{base}/documents').json()
        assert [row['id'] for row in docs] == [original['id']]
        assert client.get('/api/km/v1/citations/' + old_chunk).status_code == 200
        assert client.post('/api/km/v1/query', json={'query': 'Historical receipts'}).json()['chunks']
