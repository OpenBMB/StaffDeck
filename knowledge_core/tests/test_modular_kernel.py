import base64
import subprocess
import sys
import time
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from knowledge_core.configuration import Configuration
from knowledge_core.runtime import Runtime
from knowledge_core.api import create_app


def wait(client, job_id):
    for _ in range(200):
        row = client.get('/api/km/v1/jobs/' + job_id).json()
        if row['status'] in {'succeeded', 'failed', 'cancelled'}: return row
        time.sleep(.01)
    raise AssertionError('Task did not terminate')


def test_minimal_has_no_optional_storage_and_real_content_flow(tmp_path):
    runtime = Runtime('sqlite:///' + str(tmp_path / 'minimal.sqlite'))
    with TestClient(create_app(runtime)) as client:
        tables = set(inspect(runtime.engine).get_table_names())
        assert tables == {'km_documents', 'km_buckets', 'km_chunks', 'km_ingest_jobs', 'km_citation_snapshots', 'km_schema_versions'}
        for table in ('km_documents', 'km_buckets', 'km_chunks'):
            assert not {'space_id', 'knowledge_base_id', 'version_id'} & {row['name'] for row in inspect(runtime.engine).get_columns(table)}
        config = client.get('/api/km/config').json()
        assert config['preset'] == 'minimal' and config['model_mode'] == 'lexical'
        for path in ('spaces', 'knowledge-bases', 'concepts', 'suggestions'):
            assert client.get('/api/km/v1/' + path).status_code == 404
        for parameter in ('space_id', 'knowledge_base_id', 'version_id'):
            assert client.get('/api/km/v1/documents', params={parameter: 'disabled'}).status_code == 422
        upload = client.post('/api/km/v1/documents', json={'filename': 'expenses.md', 'content_base64': base64.b64encode(b'# Expense reimbursement\n\nExpense reimbursement requires original receipts within 30 days.').decode()})
        assert upload.status_code == 202, upload.text
        job = wait(client, upload.json()['id'])
        assert job['status'] == 'succeeded', job
        docs = client.get('/api/km/v1/documents').json()
        assert len(docs) == 1
        result = client.post('/api/km/v1/query', json={'query': 'Expense reimbursement receipts'}).json()
        assert result['evidence_pack'], result
        citation_id = result['chunks'][0]['id']
        assert client.get('/api/km/v1/citations/' + citation_id).status_code == 200
        payload = {'expected_updated_at': docs[0]['updated_at'], 'content_md': '# Expense policy\n\nKeep original receipts.'}
        edited = client.put('/api/km/v1/documents/' + docs[0]['id'], json=payload)
        assert edited.status_code == 200, edited.text
        assert client.put('/api/km/v1/documents/' + docs[0]['id'], json=payload).status_code == 409
        assert client.get('/api/km/v1/citations/' + citation_id).status_code == 200


def test_fresh_minimal_process_does_not_import_optional_implementations():
    code = "from knowledge_core.runtime import Runtime; from knowledge_core.api import create_app, Upload; from knowledge_core.client import KnowledgeClient; import sys; r=Runtime('sqlite://'); r.initialize(); create_app(r); assert not any(n in sys.modules for n in ['knowledge_core.models','knowledge_core.service','knowledge_core.okf','knowledge_core.versions','knowledge_core.publication','knowledge_core.full_api']); r.close()"
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_kernel_preserves_section_and_related_paragraph_evidence(tmp_path):
    body = '# Expense policy\n\n' + 'Receipts must be retained for expense approval. ' * 65
    runtime = Runtime('sqlite:///' + str(tmp_path / 'sections.sqlite'))
    with TestClient(create_app(runtime)) as client:
        task = client.post('/api/km/v1/documents', json={'filename': 'expense.md',
            'content_base64': base64.b64encode(body.encode()).decode()}).json()
        job = wait(client, task['id'])
        assert job['status'] == 'succeeded', job
        chunks = client.get('/api/km/v1/documents/' + job['document_id'] + '/chunks').json()
        related = [row for row in chunks if row['metadata']['related_group_id']]
        assert len(related) > 1
        for row in related:
            metadata = row['metadata']
            assert row['id'] in metadata['related_chunk_ids']
            assert metadata['related_chunk_count'] <= 6
            assert metadata['section_path'] and metadata['context_window']
            assert row['source_ref'].startswith('expense.md / ')
        buckets = client.get('/api/km/v1/documents/' + job['document_id'] + '/buckets').json()
        assert all(row['metadata']['quality'] and row['metadata']['representative_chunk_ids'] for row in buckets)
        result = client.post('/api/km/v1/query', json={'query': 'expense approval', 'max_chunks': 1}).json()
        assert len(result['chunks']) > 1
        assert result['evidence_pack']


def test_module_dependency_configuration_rejects_implicit_enabling():
    with pytest.raises(ValueError, match='versions requires catalog'):
        Configuration(modules={'versions': True})
    with pytest.raises(ValueError, match='publication requires discovery'):
        Configuration(modules={'publication': True})
    with pytest.raises(ValueError, match='requires an identity provider'):
        Runtime('sqlite://', configuration=Configuration(modules={'authentication': True}))


@pytest.mark.parametrize('modules', [
    {'authentication': True}, {'catalog': True}, {'catalog': True, 'versions': True},
    {'concepts': True}, {'discovery': True}, {'concepts': True, 'discovery': True},
    {'spaces': True, 'catalog': True},
])
def test_optional_compositions_have_real_content_flow_and_owned_tables(tmp_path, modules):
    from knowledge_core.identity import StaticBearerIdentity, Principal
    identity = StaticBearerIdentity({'test': Principal('test')}) if modules.get('authentication') else None
    runtime = Runtime('sqlite:///' + str(tmp_path / 'combo.sqlite'), configuration=Configuration(modules=modules), identity=identity)
    headers = {'Authorization': 'Bearer test'} if identity else {}
    with TestClient(create_app(runtime), headers=headers) as client:
        if identity:
            assert client.get('/api/km/v1/identity', headers={'Authorization': ''}).status_code == 401
        scope = {}
        if modules.get('spaces'):
            assert client.post('/api/km/v1/spaces', json={'id': 'one', 'name': 'One'}).status_code == 201
            scope['space_id'] = 'one'
        if modules.get('catalog'):
            base = client.post('/api/km/v1/knowledge-bases', json={**scope, 'name': 'Policies'})
            assert base.status_code == 201, base.text
            scope['knowledge_base_id'] = base.json()['id']
        response = client.post('/api/km/v1/documents', json={**scope, 'filename': 'policy.md',
            'content_base64': base64.b64encode(b'# Expense policy\n\nExpense receipts are required within 30 days.').decode()})
        assert response.status_code == 202, response.text
        for _ in range(200):
            job = client.get('/api/km/v1/jobs/' + response.json()['id'], params=scope).json()
            if job['status'] in {'succeeded', 'failed', 'cancelled'}: break
            time.sleep(.01)
        assert job['status'] == 'succeeded', job
        result = client.post('/api/km/v1/query', json={**scope, 'query': 'Expense receipts'}).json()
        assert result['evidence_pack'], result
        assert client.get('/api/km/v1/citations/' + result['chunks'][0]['id'], params=scope).status_code == 200
        if modules.get('concepts'):
            concepts = client.get('/api/km/v1/concepts', params=scope)
            assert concepts.status_code == 200 and concepts.json(), concepts.text
            assert client.get('/api/km/v1/okf', params=scope).status_code == 200
            assert client.post('/api/km/v1/okf/lint', params=scope).status_code == 200
        if modules.get('versions'):
            path = '/api/km/v1/knowledge-bases/' + scope['knowledge_base_id']
            branch = client.get(path + '/branch').json()
            copied = client.post(path + '/versions/copy', json={'expected_updated_at': branch['updated_at']})
            assert copied.status_code == 200, copied.text
            branch = client.get(path + '/branch').json()
            published = client.post(path + '/versions/publish', json={'expected_updated_at': branch['updated_at']})
            assert published.status_code == 200, published.text
            branch = client.get(path + '/branch').json()
            restored = client.post(path + '/versions/rollback', json={'expected_updated_at': branch['updated_at']})
            assert restored.status_code == 200, restored.text
        tables = set(inspect(runtime.engine).get_table_names())
        assert ('km_spaces' in tables) == bool(modules.get('spaces'))
        assert ('km_catalogs' in tables) == bool(modules.get('catalog'))
        assert ('km_versions' in tables) == bool(modules.get('versions'))
        assert ('km_concepts' in tables) == bool(modules.get('concepts'))
        assert ('km_suggestions' in tables) == bool(modules.get('discovery'))
        assert 'km_publications' not in tables


def test_module_disable_requires_offline_conversion(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'existing.sqlite')
    runtime = Runtime(url, configuration=Configuration(modules={'catalog': True}))
    runtime.initialize(); runtime.close()
    runtime = Runtime(url)
    try:
        with pytest.raises(ValueError, match='offline conversion'): runtime.initialize()
    finally: runtime.close()


def test_cancel_inflight_parser_rolls_back_content_and_restart_clears_payload(tmp_path):
    from threading import Event
    from sqlalchemy import select
    from knowledge_core.kernel import storage as s
    started, release = Event(), Event()
    class Parser:
        def parse(self, filename, content):
            started.set(); assert release.wait(5)
            return content.decode(), 'md'
    path = tmp_path / 'cancel.sqlite'
    runtime = Runtime('sqlite:///' + str(path), parser=Parser())
    with TestClient(create_app(runtime)) as client:
        response = client.post('/api/km/v1/documents', json={'filename': 'cancel.md', 'content_base64': base64.b64encode(b'Cancel this content').decode()})
        assert started.wait(5)
        assert client.post('/api/km/v1/jobs/' + response.json()['id'] + '/cancel').json()['status'] == 'cancelled'
        release.set()
        runtime.executor.shutdown(wait=True)
        assert client.get('/api/km/v1/documents').json() == []
        with runtime.engine.begin() as db:
            row = db.execute(select(s.jobs).where(s.jobs.c.id == response.json()['id'])).mappings().one()
            assert row['status'] == 'cancelled' and row['payload'] is None
            db.execute(s.jobs.insert().values(id='interrupted', filename='restart.md', status='running', stage='processing', progress=.4,
                payload={'content_base64': 'sensitive'}, context={}, principal={}, created_at=s.now(), updated_at=s.now()))
    with TestClient(create_app(Runtime('sqlite:///' + str(path)))) as client:
        row = client.get('/api/km/v1/jobs/interrupted').json()
        assert row['status'] == 'failed' and row['error'] == 'PROCESS_RESTART_INTERRUPTED'


def test_discovery_and_publication_without_concepts_have_review_and_idempotent_receipts(tmp_path):
    from knowledge_core.ports import ModelConfig
    calls = []
    class Model:
        def generate_json(self, config, prompt, payload):
            if 'discover' in prompt.lower() or 'discoveries' in prompt:
                return {'discoveries': [{'suggestion_type': 'generic', 'title': 'Expense approval', 'payload': {'steps': ['Review receipts']}}]}
            return {'buckets': [{'title': 'Expenses', 'summary': 'Receipts', 'section_ids': ['sec_1']}]}
    class Publisher:
        def publish(self, suggestion, idempotency_key):
            assert suggestion['tenant_id'] == 'local'
            assert suggestion['payload_json'] == {'steps': ['Review receipts']}
            assert idempotency_key == 'km:local:' + suggestion['id']
            calls.append(idempotency_key)
            return {'id': 'external-one'}
    runtime = Runtime('sqlite:///' + str(tmp_path / 'publication.sqlite'), configuration=Configuration(modules={'discovery': True, 'publication': True}),
        model=Model(), model_config=ModelConfig('unused', 'test', ''), publisher=Publisher())
    with TestClient(create_app(runtime)) as client:
        job = client.post('/api/km/v1/documents', json={'filename': 'expense.md', 'content_base64': base64.b64encode(b'# Expenses\n\nKeep receipts').decode()}).json()
        assert wait(client, job['id'])['status'] == 'succeeded'
        rows = client.get('/api/km/v1/suggestions').json()
        assert len(rows) == 1
        path = '/api/km/v1/suggestions/' + rows[0]['id']
        assert client.post(path + '/publish').status_code == 409
        assert client.post(path + '/review').json() == {'status': 'reviewed', 'published': False}
        assert client.post(path + '/export').json()['status'] == 'reviewed'
        assert calls == []
        assert client.post(path + '/publish').json() == {'id': 'external-one'}
        assert client.post(path + '/publish').json() == {'id': 'external-one'}
        assert len(calls) == 1
        assert 'km_concepts' not in inspect(runtime.engine).get_table_names()


def test_minimal_errors_and_read_only_do_not_load_optional_implementations(tmp_path):
    from knowledge_core.identity import StaticBearerIdentity, Principal
    class FailedParser:
        def parse(self, filename, content): raise ValueError('PARSER_UNAVAILABLE')
    runtime = Runtime('sqlite:///' + str(tmp_path / 'errors.sqlite'), parser=FailedParser())
    with TestClient(create_app(runtime)) as client:
        assert client.post('/api/km/v1/documents', json={'filename': 'invalid.md', 'content_base64': '!not base64'}).status_code == 422
        job = client.post('/api/km/v1/documents', json={'filename': 'parse.md', 'content_base64': 'eA=='}).json()
        assert wait(client, job['id'])['error'] == 'PARSER_UNAVAILABLE'
    runtime = Runtime('sqlite:///' + str(tmp_path / 'auth.sqlite'), configuration=Configuration(modules={'authentication': True}),
        identity=StaticBearerIdentity({'reader': Principal('reader', permissions=frozenset({'read'}))}))
    with TestClient(create_app(runtime), headers={'Authorization': 'Bearer reader'}) as client:
        assert client.post('/api/km/v1/documents', json={'filename': 'denied.md', 'content_base64': 'eA=='}).status_code == 403
        assert client.get('/api/km/v1/documents').json() == []


def test_versions_in_shared_catalog_are_isolated_by_space(tmp_path):
    from knowledge_core.kernel import storage as s
    runtime = Runtime('sqlite:///' + str(tmp_path / 'versions.sqlite'), configuration=Configuration(modules={'spaces': True, 'catalog': True, 'versions': True}))
    with TestClient(create_app(runtime)) as client:
        for space in ('one', 'two'):
            assert client.post('/api/km/v1/spaces', json={'id': space, 'name': space}).status_code == 201
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Shared', 'space_id': 'one'}).json()
        with runtime.engine.begin() as db:
            db.execute(runtime.plugins['catalog'].bindings.insert().values(knowledge_base_id=base['id'], space_id='two'))
            runtime.plugins['versions'].branch(db, {'space_id': 'two', 'knowledge_base_id': base['id']}, create=True)
        path = '/api/km/v1/knowledge-bases/' + base['id']
        branch = client.get(path + '/branch?space_id=one').json()
        copied = client.post(path + '/versions/copy?space_id=one', json={'expected_updated_at': branch['updated_at']}).json()
        assert client.get(path + '/versions?space_id=two').json() == []
        assert client.get('/api/km/v1/documents', params={'space_id': 'two', 'version_id': copied['id']}).status_code == 404
        branch = client.get(path + '/branch?space_id=two').json()
        assert client.post(path + '/versions/rollback?space_id=two', json={'version_id': copied['id'], 'expected_updated_at': branch['updated_at']}).status_code == 404


def test_binding_copy_isolated_edit_and_hidden_binding_excludes_query(tmp_path):
    runtime = Runtime('sqlite:///' + str(tmp_path / 'bindings.sqlite'), configuration=Configuration(modules={'spaces': True, 'catalog': True, 'versions': True}))
    with TestClient(create_app(runtime)) as client:
        for space in ('one', 'two'):
            client.post('/api/km/v1/spaces', json={'id': space, 'name': space})
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Shared', 'space_id': 'one'}).json()
        job = client.post('/api/km/v1/documents', json={'knowledge_base_id': base['id'], 'space_id': 'one', 'filename': 'expense.md', 'content_base64': 'RXhwZW5zZSByZWNlaXB0cw=='}).json()
        for _ in range(200):
            row = client.get('/api/km/v1/jobs/' + job['id'] + '?space_id=one').json()
            if row['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        assert row['status'] == 'succeeded'
        original = client.get('/api/km/v1/documents?space_id=one').json()[0]
        path = '/api/km/v1/knowledge-bases/' + base['id']
        response = client.post(path + '/bindings?source_space_id=one', json={'space_id': 'two'})
        assert response.status_code == 201, response.text
        clone = client.get('/api/km/v1/documents?space_id=two').json()[0]
        assert clone['id'] != original['id']
        response = client.put('/api/km/v1/documents/' + clone['id'] + '?space_id=two', json={'expected_updated_at': clone['updated_at'], 'content_md': 'Changed second branch'})
        assert response.status_code == 200, response.text
        assert client.get('/api/km/v1/documents/' + original['id'] + '?space_id=one').json()['content'] == 'Expense receipts'
        assert client.put(path + '/binding?space_id=two', json={'hidden': True}).status_code == 200
        assert client.get('/api/km/v1/documents?space_id=two').json() == []
        assert client.post('/api/km/v1/query', json={'space_id': 'two', 'query': 'Changed second'}).json()['chunks'] == []
        assert client.put(path + '/binding?space_id=two', json={'hidden': False}).status_code == 200
        assert client.get('/api/km/v1/documents?space_id=two').json()[0]['id'] == clone['id']


def test_independent_concept_okf_roundtrip_preserves_paths(tmp_path):
    configuration = Configuration(modules={'concepts': True})
    with TestClient(create_app(Runtime('sqlite:///' + str(tmp_path / 'source.sqlite'), configuration=configuration))) as client:
        concept = client.put('/api/km/v1/concepts/policies/expense', json={'content_md': '---\ntitle: Expenses\n---\n\nKeep [receipts](receipts.md).'}).json()
        assert concept['concept_id'] == 'policies/expense'
        bundle = client.get('/api/km/v1/okf').content
    with TestClient(create_app(Runtime('sqlite:///' + str(tmp_path / 'target.sqlite'), configuration=configuration))) as client:
        response = client.post('/api/km/v1/okf', json={'filename': 'bundle.zip', 'content_base64': base64.b64encode(bundle).decode()})
        assert response.status_code == 200, response.text
        assert response.json()['concepts'][0]['concept_id'] == concept['concept_id']
        assert response.json()['concepts'][0]['content_md'] == concept['content_md']
        query = client.post('/api/km/v1/query', json={'query': 'receipts'}).json()
        assert query['selected_concepts'] and query['evidence_pack']
        citation = client.get('/api/km/v1/citations/' + query['evidence_pack'][0]['chunk_id'])
        assert citation.status_code == 200 and citation.json()['content'] == concept['content_md']


def test_invalid_okf_zip_returns_input_error_and_preserves_concepts(tmp_path):
    configuration = Configuration(modules={'concepts': True})
    with TestClient(create_app(Runtime('sqlite:///' + str(tmp_path / 'invalid.sqlite'), configuration=configuration))) as client:
        assert client.put('/api/km/v1/concepts/policy', json={'content_md': '# Policy'}).status_code == 200
        response = client.post('/api/km/v1/okf', json={'filename': 'invalid.zip', 'content_base64': 'bm90IGEgemlw'})
        assert response.status_code == 422
        assert len(client.get('/api/km/v1/concepts').json()) == 1


def test_chunk_edit_updates_retrieved_evidence_and_preserves_original_citation(tmp_path):
    with TestClient(create_app(Runtime('sqlite:///' + str(tmp_path / 'chunks.sqlite')))) as client:
        task = client.post('/api/km/v1/documents', json={'filename': 'policy.md', 'content_base64': 'RXhwZW5zZSByZWNlaXB0cw=='}).json()
        for _ in range(200):
            job = client.get('/api/km/v1/jobs/' + task['id']).json()
            if job['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        assert job['status'] == 'succeeded'
        original = client.get('/api/km/v1/documents/' + job['document_id']).json()
        chunk = client.get('/api/km/v1/documents/' + original['id'] + '/chunks').json()[0]
        changes = {'expected_updated_at': chunk['updated_at'], 'content_md': 'Expense receipts revised evidence'}
        edited = client.put('/api/km/v1/chunks/' + chunk['id'], json=changes)
        assert edited.status_code == 200, edited.text
        assert client.put('/api/km/v1/chunks/' + chunk['id'], json=changes).status_code == 409
        assert client.put('/api/km/v1/documents/' + original['id'], json={'expected_updated_at': original['updated_at'], 'title': 'Stale'}).status_code == 409
        query = client.post('/api/km/v1/query', json={'query': 'Expense receipts'}).json()
        hit = query['chunks'][0]
        assert hit['citation_id'] != chunk['id']
        assert query['evidence_pack'][0]['chunk_id'] == hit['citation_id']
        assert client.get('/api/km/v1/citations/' + hit['citation_id']).json()['content'] == changes['content_md']
        assert client.get('/api/km/v1/citations/' + chunk['id']).json()['content'] == 'Expense receipts'
        assert client.put('/api/km/v1/chunks/' + chunk['id'], json={'expected_updated_at': edited.json()['updated_at'], 'title': 'Invalid'}).status_code == 422


def test_version_snapshots_restore_optional_content_and_reject_stale_branch(tmp_path):
    from knowledge_core.kernel import storage as s
    configuration = Configuration(modules={'catalog': True, 'versions': True, 'concepts': True, 'discovery': True})
    runtime = Runtime('sqlite:///' + str(tmp_path / 'snapshot.sqlite'), configuration=configuration)
    with TestClient(create_app(runtime)) as client:
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Policies'}).json()
        scope = {'knowledge_base_id': base['id']}
        path = '/api/km/v1/knowledge-bases/' + base['id']
        stale_branch = client.get(path + '/branch').json()
        task = client.post('/api/km/v1/documents', json={**scope, 'filename': 'policy.md', 'content_base64': 'RXhwZW5zZSByZWNlaXB0cw=='}).json()
        assert wait(client, task['id'])['status'] == 'succeeded'
        doc = client.get('/api/km/v1/documents', params=scope).json()[0]
        concept = client.put('/api/km/v1/concepts/policies/manual', json={**scope, 'document_id': doc['id'], 'content_md': '# Expense manual\n\nKeep receipts.'}).json()
        with runtime.engine.begin() as db:
            plugin = runtime.plugins['discovery']
            db.execute(plugin.suggestions.insert().values(id='suggestion', document_id=doc['id'], title='Review', suggestion_type='generic', status='confirmed',
                payload={'document_id': doc['id']}, source_refs=[{'document_id': doc['id']}], created_at=s.now(), updated_at=s.now()))
        assert client.post(path + '/versions/copy', json={'expected_updated_at': stale_branch['updated_at']}).status_code == 409
        branch = client.get(path + '/branch').json()
        snapshot = client.post(path + '/versions/copy', json={'expected_updated_at': branch['updated_at']}).json()
        edited = client.put('/api/km/v1/concepts/policies/manual', json={**scope, 'document_id': doc['id'], 'content_md': '# Changed concept', 'expected_updated_at': concept['updated_at']})
        assert edited.status_code == 200
        historical = client.get('/api/km/v1/concepts', params={**scope, 'version_id': snapshot['id']}).json()
        assert any(row['content_md'] == concept['content_md'] for row in historical)
        branch = client.get(path + '/branch').json()
        restored = client.post(path + '/versions/rollback', json={'version_id': snapshot['id'], 'expected_updated_at': branch['updated_at']})
        assert restored.status_code == 200, restored.text
        clone_id = restored.json()['document_mapping'][doc['id']]
        concepts = client.get('/api/km/v1/concepts', params=scope).json()
        assert any(row['document_id'] == clone_id and row['content_md'] == concept['content_md'] for row in concepts)
        suggestions = client.get('/api/km/v1/suggestions', params=scope).json()
        assert len(suggestions) == 1 and suggestions[0]['status'] == 'confirmed'
        assert suggestions[0]['payload']['document_id'] == clone_id
        assert suggestions[0]['source_refs'] == [{'document_id': clone_id}]


@pytest.mark.parametrize('modules', [{}, {'authentication': True}, {'spaces': True}, {'catalog': True},
    {'catalog': True, 'versions': True}, {'concepts': True}, {'discovery': True},
    {'concepts': True, 'discovery': True}, {'spaces': True, 'catalog': True, 'versions': True},
    {'spaces': True, 'catalog': True, 'versions': True, 'concepts': True, 'discovery': True, 'publication': True}])
def test_cancel_restart_and_concurrency_across_compositions(tmp_path, modules):
    from threading import Event
    from knowledge_core.identity import StaticBearerIdentity, Principal
    from knowledge_core.kernel import storage as s
    from knowledge_core.kernel.api import create_app as create_kernel_app
    started, release = Event(), Event()
    class Parser:
        def parse(self, filename, content):
            if filename == 'blocked.md': started.set(); assert release.wait(5)
            return content.decode(), 'md'
    identity = StaticBearerIdentity({'test': Principal('test')}) if modules.get('authentication') else None
    headers = {'Authorization': 'Bearer test'} if identity else {}
    configuration = Configuration(modules=modules)
    url = 'sqlite:///' + str(tmp_path / 'combination.sqlite')
    runtime = Runtime(url, configuration=configuration, identity=identity, parser=Parser())
    scope = {}
    with TestClient(create_kernel_app(runtime), headers=headers) as client:
        if modules.get('spaces'):
            client.post('/api/km/v1/spaces', json={'id': 'one', 'name': 'One'}); scope['space_id'] = 'one'
        if modules.get('catalog'):
            base = client.post('/api/km/v1/knowledge-bases', json={**scope, 'name': 'Policies'})
            assert base.status_code == 201, base.text
            scope['knowledge_base_id'] = base.json()['id']
        task = client.post('/api/km/v1/documents', json={**scope, 'filename': 'blocked.md', 'content_base64': 'eA=='}).json()
        assert started.wait(5)
        assert client.post('/api/km/v1/jobs/' + task['id'] + '/cancel', params=scope).json()['status'] == 'cancelled'
        release.set(); runtime.executor.shutdown(wait=True)
        assert client.get('/api/km/v1/documents', params=scope).json() == []
        with runtime.engine.begin() as db:
            db.execute(s.jobs.insert().values(id='interrupted', filename='restart.md', status='running', stage='parsing', progress=.1,
                context=scope, principal={}, payload={'content_base64': 'eA=='}, created_at=s.now(), updated_at=s.now()))
    runtime = Runtime(url, configuration=configuration, identity=identity)
    with TestClient(create_kernel_app(runtime), headers=headers) as client:
        assert client.get('/api/km/v1/jobs/interrupted', params=scope).json()['error'] == 'PROCESS_RESTART_INTERRUPTED'
        task = client.post('/api/km/v1/documents', json={**scope, 'filename': 'normal.md', 'content_base64': 'eA=='}).json()
        for _ in range(200):
            row = client.get('/api/km/v1/jobs/' + task['id'], params=scope).json()
            if row['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        assert row['status'] == 'succeeded'
        doc = client.get('/api/km/v1/documents', params=scope).json()[0]
        changes = {'expected_updated_at': doc['updated_at'], 'content_md': 'Updated content'}
        path = '/api/km/v1/documents/' + doc['id']
        assert client.put(path, params=scope, json=changes).status_code == 200
        assert client.put(path, params=scope, json=changes).status_code == 409
