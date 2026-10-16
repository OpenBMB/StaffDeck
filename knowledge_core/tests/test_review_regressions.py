import base64

from fastapi.testclient import TestClient
from sqlalchemy import select

from knowledge_core.api import create_app
from knowledge_core.kernel import storage as s
from support import full_runtime as Runtime
from test_independent_workflow import wait_job


def test_wiki_edit_cannot_mutate_a_published_document(tmp_path):
    with TestClient(create_app(Runtime(f"sqlite:///{tmp_path / 'published.sqlite'}"))) as client:
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Published'}).json()['id']
        job = client.post(f'/api/km/v1/knowledge-bases/{base}/documents', json={
            'filename': 'policy.md', 'content_base64': base64.b64encode(b'# Policy\n\nKeep receipts.').decode(),
        }).json()
        document = wait_job(client, job['id'])['document_id']
        concept = client.get(f'/api/km/v1/documents/{document}/concepts').json()[0]
        path = f"/api/km/v1/knowledge-bases/{base}/concepts/{concept['concept_id']}"
        assert client.post(f'/api/km/v1/knowledge-bases/{base}/versions/publish', json={}).status_code == 200
        response = client.put(path, json={'content_md': '# Changed',
                                         'expected_updated_at': concept['updated_at']})
        assert response.status_code == 409, response.text
        assert client.get(path).json()['content_md'] == concept['content_md']


def test_permanent_delete_removes_detached_wiki_and_lint_records(tmp_path):
    runtime = Runtime(f"sqlite:///{tmp_path / 'delete.sqlite'}")
    with TestClient(create_app(runtime)) as client:
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Delete'}).json()['id']
        response = client.put(f'/api/km/v1/knowledge-bases/{base}/concepts/topics/standalone',
                              json={'content_md': '# Orphan\n\n[[topics/missing]]', 'create_only': True})
        assert response.status_code == 200, response.text
        assert client.post(f'/api/km/v1/knowledge-bases/{base}/okf/lint').json()['issue_count'] > 0
        assert client.delete(f'/api/km/v1/knowledge-bases/{base}?permanent=true').status_code == 200
        concepts, discovery = runtime.plugins['concepts'], runtime.plugins['discovery']
        with runtime.engine.connect() as db:
            for table in (concepts.concepts, concepts.contexts, discovery.suggestions, discovery.contexts, s.citations):
                assert db.execute(select(table)).first() is None, table.name


def test_detached_wiki_edit_cannot_mutate_a_published_version(tmp_path):
    with TestClient(create_app(Runtime(f"sqlite:///{tmp_path / 'detached.sqlite'}"))) as client:
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Published'}).json()['id']
        path = f'/api/km/v1/knowledge-bases/{base}/concepts/topics/standalone'
        before = client.put(path, json={'content_md': '# Original', 'create_only': True}).json()
        assert client.post(f'/api/km/v1/knowledge-bases/{base}/versions/publish', json={}).status_code == 200
        response = client.put(path, json={'content_md': '# Changed', 'expected_updated_at': before['updated_at']})
        assert response.status_code == 409, response.text
        assert client.get(path).json()['content_md'] == before['content_md']


def test_detached_wiki_survives_binding_and_copy_into_another_space(tmp_path):
    with TestClient(create_app(Runtime(f"sqlite:///{tmp_path / 'binding.sqlite'}"))) as client:
        assert client.post('/api/km/v1/spaces', json={'id': 'other', 'name': 'Other'}).status_code == 201
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Shared'}).json()['id']
        path = f'/api/km/v1/knowledge-bases/{base}/concepts/topics/shared'
        original = client.put(path, json={'content_md': '# Shared Wiki', 'create_only': True}).json()
        response = client.post(f'/api/km/v1/knowledge-bases/{base}/bindings', json={'space_id': 'other'})
        assert response.status_code == 200, response.text
        response = client.get(path, params={'space_id': 'other'})
        assert response.status_code == 200, response.text
        assert response.json()['content_md'] == original['content_md']
        assert client.post(f'/api/km/v1/knowledge-bases/{base}/versions/copy',
                           params={'space_id': 'other'}, json={}).status_code == 200
        copied = client.get(path, params={'space_id': 'other'}).json()
        assert copied['id'] != original['id']
        edited = client.put(path, params={'space_id': 'other'},
                            json={'content_md': '# Independent', 'expected_updated_at': copied['updated_at']})
        assert edited.status_code == 200, edited.text
        assert client.get(path).json()['content_md'] == original['content_md']
