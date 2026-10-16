"""Agent writes must preserve revisions, retrieval visibility and citation history."""
import base64
import time
import pytest
from fastapi.testclient import TestClient
from knowledge_core.api import create_app
from knowledge_core.configuration import Configuration
from knowledge_core.runtime import Runtime


@pytest.mark.parametrize('full', [False, True])
def test_conditional_delete_and_wiki_revision_contract(tmp_path, full):
    configuration = Configuration('full') if full else Configuration(modules={'concepts': True})
    with TestClient(create_app(Runtime('sqlite:///' + str(tmp_path / 'knowledge.sqlite'), configuration=configuration))) as client:
        scope = {}
        upload = '/api/km/v1/documents'
        if full:
            base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Agent acceptance'}).json()['id']
            scope['knowledge_base_id'] = base
            upload = f'/api/km/v1/knowledge-bases/{base}/documents'
        task = client.post(upload, json={'filename': 'receipts.md', 'content_base64': base64.b64encode(b'# Expense receipts\n\nKeep receipt token ZX413.').decode()}).json()
        for _ in range(200):
            job = client.get('/api/km/v1/jobs/' + task['id']).json()
            if job['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        assert job['status'] == 'succeeded', job
        path = '/api/km/v1/documents/' + job['document_id']
        document = client.get(path).json()
        query = lambda: client.post('/api/km/v1/query', json={'query': 'ZX413 receipt', 'use_model': False}).json()
        initial = query()
        citation = initial['chunks'][0]['id']
        wiki = f'/api/km/v1/knowledge-bases/{base}/concepts/agent/receipts' if full else '/api/km/v1/concepts/agent/receipts'
        body = {'document_id': document['id'], 'content_md': '# Agent receipts\n\nZX413 receipt policy.', 'create_only': True}
        created = client.put(wiki, json=body)
        assert created.status_code == 200, created.text
        assert client.put(wiki, json=body).status_code == 409
        edited = client.put(wiki, json={**body, 'create_only': False, 'expected_updated_at': created.json()['updated_at'], 'content_md': '# Revised\n\nZX413 revised receipts.'})
        assert edited.status_code == 200, edited.text
        assert client.put(wiki, json={**body, 'create_only': False, 'expected_updated_at': created.json()['updated_at']}).status_code == 409
        assert client.put(wiki + '-missing', json={**body, 'create_only': False, 'expected_updated_at': created.json()['updated_at']}).status_code == 409
        assert client.delete(path, params={'expected_updated_at': 'stale'}).status_code == 409
        assert query()['chunks']
        # Wiki edits can advance the document head; use an actual fresh read.
        revision = client.get(path).json()['updated_at']
        assert client.delete(path, params={'expected_updated_at': revision}).status_code == 200
        assert client.delete(path, params={'expected_updated_at': revision}).status_code == 409
        after = query()
        assert not after['chunks'] and not after.get('selected_concepts'), after
        assert client.get('/api/km/v1/citations/' + citation).status_code == 200
        assert client.delete(path).status_code == 200  # Legacy compatibility.
