import base64
import sqlite3
from pathlib import Path
import time
import pytest
from fastapi.testclient import TestClient
from knowledge_core.api import create_app
from knowledge_core.configuration import Configuration
from knowledge_core.conversion import convert, restore, preflight
from knowledge_core.runtime import Runtime


def test_reference_remapping_preserves_token_boundaries_and_does_not_cascade():
    from knowledge_core.references import remap
    mapping = {'document': 'document-new', 'bucket': 'bucket-new', 'document-new': 'other'}
    value = {'related_group_id': 'document:bucket:section:0',
             'citation': '/documents/document/chunks/bucket', 'unchanged': 'document-long'}
    assert remap(value, mapping) == {'related_group_id': 'document-new:bucket-new:section:0',
        'citation': '/documents/document-new/chunks/bucket-new', 'unchanged': 'document-long'}


def test_offline_catalog_downgrade_preserves_content_citations_and_restore_guard(tmp_path):
    path = tmp_path / 'active.sqlite'
    runtime = Runtime('sqlite:///' + str(path), configuration=Configuration(modules={'catalog': True, 'versions': True}))
    with TestClient(create_app(runtime)) as client:
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Policies'}).json()
        uploaded = client.post('/api/km/v1/documents', json={'knowledge_base_id': base['id'], 'filename': 'expense.md',
            'content_base64': base64.b64encode(b'# Expenses\n\nExpense receipts must be retained.').decode()}).json()
        for _ in range(200):
            job = client.get('/api/km/v1/jobs/' + uploaded['id']).json()
            if job['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        assert job['status'] == 'succeeded', job
        result = client.post('/api/km/v1/query', json={'query': 'Expense receipts'}).json()
        citation = result['chunks'][0]['id']
    report = convert(path, Configuration(), tmp_path / 'backups')
    assert report['ok'] and report['counts']['documents'] == 1
    backup = Path(report['backup'])
    assert (backup / 'original.sqlite').is_file()
    with TestClient(create_app(Runtime('sqlite:///' + str(path)))) as client:
        assert len(client.get('/api/km/v1/documents').json()) == 1
        assert client.get('/api/km/v1/citations/' + citation).status_code == 200
        with sqlite3.connect(path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'km_catalogs' not in tables and 'km_branches' not in tables
    assert restore(path, backup)['ok']
    report = convert(path, Configuration(), tmp_path / 'backups')
    with TestClient(create_app(Runtime('sqlite:///' + str(path)))) as client:
        doc = client.get('/api/km/v1/documents').json()[0]
        assert client.put('/api/km/v1/documents/' + doc['id'], json={'expected_updated_at': doc['updated_at'], 'title': 'Changed'}).status_code == 200
    with pytest.raises(ValueError, match='New writes exist'): restore(path, Path(report['backup']))


def test_preflight_rejects_active_tasks_without_replacing_database(tmp_path):
    from knowledge_core.kernel import storage as s
    path = tmp_path / 'active.sqlite'
    runtime = Runtime('sqlite:///' + str(path)); runtime.initialize()
    with runtime.engine.begin() as db:
        db.execute(s.jobs.insert().values(id='running', filename='test.md', status='running', stage='parsing', progress=0,
            context={}, principal={}, created_at=s.now(), updated_at=s.now()))
    runtime.close()
    assert not preflight(path, Configuration())['ok']
    assert not convert(path, Configuration(), tmp_path / 'backups')['ok']
    assert not (tmp_path / 'backups').exists()


def test_revision_deduplication_keeps_distinct_branches_and_old_evidence(tmp_path):
    from knowledge_core.kernel import storage as s
    path = tmp_path / 'branches.sqlite'
    runtime = Runtime('sqlite:///' + str(path), configuration=Configuration(modules={'catalog': True, 'versions': True, 'spaces': True}))
    runtime.initialize()
    with runtime.engine.begin() as db:
        catalog = runtime.plugins['catalog']; spaces = runtime.plugins['spaces']; versions = runtime.plugins['versions']
        db.execute(catalog.catalogs.insert().values(id='base', name='Same title', description='', status='active', created_at=s.now(), updated_at=s.now()))
        for space in ('a', 'b'):
            db.execute(spaces.spaces.insert().values(id=space, name=space, tenant_id='local'))
            db.execute(catalog.bindings.insert().values(knowledge_base_id='base', space_id=space))
            db.execute(versions.branches.insert().values(id='branch-' + space, knowledge_base_id='base', updated_at=s.now()))
            db.execute(versions.scopes.insert().values(branch_id='branch-' + space, space_id=space))
        for resource_id, revision, body in [('one', 'rev1', 'Expense receipts'), ('shared', 'rev1', 'Expense receipts'), ('different', 'rev2', 'Expense approval'), ('history', 'rev0', 'Old expense')]:
            db.execute(s.documents.insert().values(id=resource_id, filename='same.md', title='Same title', file_type='md', status='ready', content=body,
                metadata={'revision_source_id': 'source', 'revision_source_updated_at': revision}, bucket_count=1, chunk_count=1, created_at=s.now(), updated_at=s.now()))
            db.execute(s.buckets.insert().values(id='bucket-' + resource_id, document_id=resource_id, title='Same title', summary=body, content=body, metadata={}, created_at=s.now(), updated_at=s.now()))
            db.execute(s.chunks.insert().values(id='chunk-' + resource_id, document_id=resource_id, bucket_id='bucket-' + resource_id, content=body, metadata={}, created_at=s.now(), updated_at=s.now()))
            db.execute(catalog.contents.insert().values(knowledge_base_id='base', document_id=resource_id))
            db.execute(spaces.contents.insert().values(space_id='a', document_id=resource_id))
        for resource_id, branch in [('one', 'a'), ('shared', 'b'), ('different', 'b')]:
            db.execute(versions.contents.insert().values(branch_id='branch-' + branch, document_id=resource_id))
    runtime.close()
    report = convert(path, Configuration(), tmp_path / 'backups')
    assert report['ok'] and report['counts']['documents'] == 2, report
    assert report['counts']['chunks'] == 2
    assert report['document_mapping']['one'] == report['document_mapping']['shared']
    with TestClient(create_app(Runtime('sqlite:///' + str(path)))) as client:
        assert len(client.get('/api/km/v1/documents').json()) == 2
        for resource_id in ('one', 'shared', 'different', 'history'):
            assert client.get('/api/km/v1/citations/chunk-' + resource_id).status_code == 200


def test_restore_restores_configuration_and_reenabled_module_blocks_restore(tmp_path):
    import json
    path = tmp_path / 'active.sqlite'
    configuration = Configuration(modules={'catalog': True})
    config_path = tmp_path / 'configuration.json'
    config_path.write_text(json.dumps(configuration.as_dict()))
    runtime = Runtime('sqlite:///' + str(path), configuration=configuration)
    runtime.initialize(); runtime.close()
    report = convert(path, Configuration(), tmp_path / 'backups', config_path)
    assert not Configuration.load(config_path).enabled('catalog')
    assert restore(path, Path(report['backup']))['ok']
    assert Configuration.load(config_path).enabled('catalog')
    report = convert(path, Configuration(), tmp_path / 'backups', config_path)
    runtime = Runtime('sqlite:///' + str(path), configuration=Configuration(modules={'discovery': True}))
    runtime.initialize(); runtime.close()
    with pytest.raises(ValueError, match='New writes exist'):
        restore(path, Path(report['backup']))


def test_configuration_replacement_failure_preserves_original_database(tmp_path, monkeypatch):
    import json
    import os
    path = tmp_path / 'active.sqlite'
    config_path = tmp_path / 'configuration.json'
    configuration = Configuration(modules={'catalog': True})
    config_path.write_text(json.dumps(configuration.as_dict()))
    runtime = Runtime('sqlite:///' + str(path), configuration=configuration)
    runtime.initialize(); runtime.close()
    original_replace = os.replace
    def fail_config(source, destination):
        if destination == config_path: raise OSError('configuration replacement interrupted')
        return original_replace(source, destination)
    monkeypatch.setattr(os, 'replace', fail_config)
    with pytest.raises(OSError, match='interrupted'):
        convert(path, Configuration(), tmp_path / 'backups', config_path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='km_catalogs'").fetchone()
    assert Configuration.load(config_path).enabled('catalog')


def test_disabling_spaces_merges_all_heads_into_catalog_branch(tmp_path):
    from knowledge_core.kernel import storage as s
    path = tmp_path / 'branches.sqlite'
    runtime = Runtime('sqlite:///' + str(path), configuration=Configuration(modules={'spaces': True, 'catalog': True, 'versions': True}))
    runtime.initialize()
    with runtime.engine.begin() as db:
        catalog, spaces, versions = (runtime.plugins[name] for name in ('catalog', 'spaces', 'versions'))
        db.execute(catalog.catalogs.insert().values(id='base', name='Policies', status='active', created_at=s.now(), updated_at=s.now()))
        for space_id in ('a', 'b'):
            scope = {'space_id': space_id, 'knowledge_base_id': 'base'}
            db.execute(spaces.spaces.insert().values(id=space_id, name=space_id, tenant_id='local'))
            db.execute(catalog.bindings.insert().values(knowledge_base_id='base', space_id=space_id))
            db.execute(s.documents.insert().values(id=space_id, filename='same.md', title='Same title', status='ready', content=space_id,
                metadata={}, bucket_count=0, chunk_count=0, created_at=s.now(), updated_at=s.now()))
            runtime.assign_content(db, space_id, scope)
    runtime.close()
    configuration = Configuration(modules={'catalog': True, 'versions': True})
    result = convert(path, configuration, tmp_path / 'backups')
    assert result['ok'], result
    with TestClient(create_app(Runtime('sqlite:///' + str(path), configuration=configuration))) as client:
        rows = client.get('/api/km/v1/documents', params={'knowledge_base_id': 'base'}).json()
        assert {row['id'] for row in rows} == {'a', 'b'}
        versions = client.get('/api/km/v1/knowledge-bases/base/versions').json()
        assert len(versions) == 1 and set(versions[0]['snapshot']) == {'a', 'b'}


def test_recovery_export_contains_new_and_changed_content_without_allowing_restore(tmp_path):
    from knowledge_core.conversion import export_changes
    import json
    path = tmp_path / 'active.sqlite'
    runtime = Runtime('sqlite:///' + str(path))
    runtime.initialize(); runtime.close()
    report = convert(path, Configuration(), tmp_path / 'backups')
    backup = Path(report['backup'])
    with TestClient(create_app(Runtime('sqlite:///' + str(path)))) as client:
        job = client.post('/api/km/v1/documents', json={'filename': 'new.md', 'content_base64': 'TmV3IGNvbnRlbnQ='}).json()
        for _ in range(200):
            current = client.get('/api/km/v1/jobs/' + job['id']).json()
            if current['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        assert current['status'] == 'succeeded', current
    output = tmp_path / 'recovery.json'
    assert export_changes(path, backup, output)['changed_documents'] == 1
    bundle = json.loads(output.read_text())
    assert bundle['tables']['km_documents'][0]['content'] == 'New content'
    assert bundle['tables']['km_chunks'] and bundle['tables']['km_ingest_jobs']
    with pytest.raises(ValueError, match='New writes exist'): restore(path, backup)


def test_missing_links_fail_preflight_without_replacing_source(tmp_path):
    from knowledge_core.kernel import storage as s
    path = tmp_path / 'invalid.sqlite'
    runtime = Runtime('sqlite:///' + str(path)); runtime.initialize()
    with runtime.engine.begin() as db:
        db.execute(s.buckets.insert().values(id='orphan', document_id='missing', title='Broken', metadata={}, created_at=s.now(), updated_at=s.now()))
    runtime.close()
    report = convert(path, Configuration(), tmp_path / 'backups')
    assert not report['ok'] and 'missing document' in report['errors'][0]
    assert not (tmp_path / 'backups').exists()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT document_id FROM km_buckets').fetchone()[0] == 'missing'


def test_interrupted_replacement_blocks_startup_and_recovers_database_and_config(tmp_path, monkeypatch):
    from knowledge_core.conversion import recover_interrupted
    import json
    import os
    path = tmp_path / 'active.sqlite'
    config = tmp_path / 'configuration.json'
    configuration = Configuration(modules={'catalog': True})
    config.write_text(json.dumps(configuration.as_dict()))
    runtime = Runtime('sqlite:///' + str(path), configuration=configuration)
    runtime.initialize(); runtime.close()
    original = os.replace
    def interrupted(source, destination):
        if destination == config: raise KeyboardInterrupt('simulated process interruption')
        return original(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', interrupted)
        with pytest.raises(KeyboardInterrupt): convert(path, Configuration(), tmp_path / 'backups', config)
    runtime = Runtime('sqlite:///' + str(path))
    try:
        with pytest.raises(ValueError, match='interrupted'): runtime.initialize()
    finally: runtime.close()
    assert recover_interrupted(path)['ok']
    assert Configuration.load(config).enabled('catalog')
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='km_catalogs'").fetchone()


def test_historical_citation_survives_directory_retaining_downgrade(tmp_path):
    path = tmp_path / 'history.sqlite'
    configuration = Configuration(modules={'catalog': True, 'versions': True})
    with TestClient(create_app(Runtime('sqlite:///' + str(path), configuration=configuration))) as client:
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Policies'}).json()
        scope = {'knowledge_base_id': base['id']}
        task = client.post('/api/km/v1/documents', json={**scope, 'filename': 'policy.md', 'content_base64': 'RXhwZW5zZSByZWNlaXB0cw=='}).json()
        for _ in range(200):
            job = client.get('/api/km/v1/jobs/' + task['id']).json()
            if job['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        citation = client.post('/api/km/v1/query', json={**scope, 'query': 'Expense receipts'}).json()['chunks'][0]['id']
        branch_path = '/api/km/v1/knowledge-bases/' + base['id']
        branch = client.get(branch_path + '/branch').json()
        version = client.post(branch_path + '/versions/copy', json={'expected_updated_at': branch['updated_at']}).json()
        branch = client.get(branch_path + '/branch').json()
        assert client.post(branch_path + '/versions/rollback', json={'expected_updated_at': branch['updated_at'], 'version_id': version['id']}).status_code == 200
    configuration = Configuration(modules={'catalog': True})
    report = convert(path, configuration, tmp_path / 'backups')
    assert report['ok'] and report['citation_mapping'][citation] == citation
    assert report['chunk_mapping'][citation] is None
    with TestClient(create_app(Runtime('sqlite:///' + str(path), configuration=configuration))) as client:
        response = client.get('/api/km/v1/citations/' + citation, params=scope)
        assert response.status_code == 200, response.text
        assert response.json()['content'] == 'Expense receipts'


def test_restore_process_interruption_preserves_journal_and_recovers_config(tmp_path, monkeypatch):
    from knowledge_core.conversion import recover_interrupted
    import json
    import os
    path = tmp_path / 'active.sqlite'
    config = tmp_path / 'configuration.json'
    original_configuration = Configuration(modules={'catalog': True})
    config.write_text(json.dumps(original_configuration.as_dict()))
    runtime = Runtime('sqlite:///' + str(path), configuration=original_configuration)
    runtime.initialize(); runtime.close()
    report = convert(path, Configuration(), tmp_path / 'backups', config)
    original_replace = os.replace
    def interrupted(source, destination):
        if destination == config: raise KeyboardInterrupt('restore interruption')
        return original_replace(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', interrupted)
        with pytest.raises(KeyboardInterrupt): restore(path, Path(report['backup']))
    assert path.with_name(path.name + '.km-conversion.json').exists()
    assert not Configuration.load(config).enabled('catalog')
    assert recover_interrupted(path)['ok']
    assert Configuration.load(config).enabled('catalog')
    assert not list(tmp_path.glob('*.before-restore-*'))
    runtime = Runtime('sqlite:///' + str(path), configuration=original_configuration)
    runtime.initialize(); runtime.close()


def test_repeated_snapshot_restore_preserves_original_revision_deduplication(tmp_path):
    from knowledge_core.kernel import storage as s
    configuration = Configuration(modules={'catalog': True, 'versions': True})
    path = tmp_path / 'copies.sqlite'
    runtime = Runtime('sqlite:///' + str(path), configuration=configuration)
    runtime.initialize()
    scope = {'knowledge_base_id': 'base'}
    with runtime.engine.begin() as db:
        catalog, versions = (runtime.plugins[name] for name in ('catalog', 'versions'))
        db.execute(catalog.catalogs.insert().values(id='base', name='Policies', status='active', created_at=s.now(), updated_at=s.now()))
        db.execute(s.documents.insert().values(id='original', filename='same.md', title='Same', status='ready', content='Receipts', metadata={},
            bucket_count=0, chunk_count=0, created_at=s.now(), updated_at=s.now()))
        runtime.assign_content(db, 'original', scope)
        original = versions.checkpoint(db, scope, 'original')
        first = versions.restore(db, versions.branch(db, scope), original, scope)['document_mapping']['original']
        copied = versions.checkpoint(db, scope, 'copy')
        second = versions.restore(db, versions.branch(db, scope), copied, scope)['document_mapping'][first]
        for document_id in ('original', first):
            db.execute(versions.contents.insert().values(branch_id=versions.branch(db, scope)['id'], document_id=document_id))
    runtime.close()
    result = convert(path, Configuration(), tmp_path / 'backups')
    assert result['ok'] and result['counts']['documents'] == 1
    assert len({result['document_mapping'][key] for key in ('original', first, second)}) == 1


@pytest.mark.parametrize('modules', [{'spaces': True}, {'catalog': True, 'versions': True}, {'spaces': True, 'catalog': True, 'versions': True}])
def test_legacy_downgrade_can_retain_space_and_catalog_versions(tmp_path, modules):
    from knowledge_core.full_runtime import Runtime as LegacyRuntime
    from knowledge_core.full_api import create_app as create_legacy_fixture_app
    path = tmp_path / 'legacy.sqlite'
    with TestClient(create_legacy_fixture_app(LegacyRuntime('sqlite:///' + str(path)))) as client:
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Policies'}).json()
        task = client.post('/api/km/v1/knowledge-bases/' + base['id'] + '/documents', json={'filename': 'policy.md', 'content_base64': 'RXhwZW5zZSByZWNlaXB0cw=='}).json()
        for _ in range(200):
            job = client.get('/api/km/v1/jobs/' + task['id']).json()
            if job['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        assert job['status'] == 'succeeded'
        document = client.get('/api/km/v1/documents/' + job['document_id']).json()
        citation = client.get('/api/km/v1/documents/' + document['id'] + '/chunks').json()[0]['id']
        assert client.post('/api/km/v1/knowledge-bases/' + base['id'] + '/versions/copy', json={}).status_code == 200
    configuration = Configuration(modules=modules)
    report = convert(path, configuration, tmp_path / 'backups')
    assert report['ok'], report
    scope = {'space_id': 'local'} if modules.get('spaces') else {}
    if modules.get('catalog'): scope['knowledge_base_id'] = base['id']
    with TestClient(create_app(Runtime('sqlite:///' + str(path), configuration=configuration))) as client:
        assert client.get('/api/km/v1/documents', params=scope).json()
        response = client.get('/api/km/v1/citations/' + citation, params=scope)
        assert response.status_code == 200, response.text
        assert response.json()['content'] == 'Expense receipts'
        if modules.get('versions'):
            assert client.get('/api/km/v1/knowledge-bases/' + base['id'] + '/versions', params=scope).json()
        with sqlite3.connect(path) as db:
            assert not any(row[0].startswith('knowledge_') for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))


def test_legacy_optional_records_retained_without_republication_or_disabled_history(tmp_path):
    from knowledge_core.full_runtime import Runtime as LegacyRuntime
    from knowledge_core.full_api import create_app as create_legacy_fixture_app
    from knowledge_core.models import KnowledgeConcept, KnowledgeDiscoverySuggestion, Publication
    from knowledge_core.conversion import read_database
    path = tmp_path / 'legacy.sqlite'
    runtime = LegacyRuntime('sqlite:///' + str(path))
    with TestClient(create_legacy_fixture_app(runtime)) as client:
        base = client.post('/api/km/v1/knowledge-bases', json={'name': 'Policies'}).json()
        task = client.post('/api/km/v1/knowledge-bases/' + base['id'] + '/documents', json={'filename': 'policy.md', 'content_base64': 'RXhwZW5zZSByZWNlaXB0cw=='}).json()
        for _ in range(200):
            job = client.get('/api/km/v1/jobs/' + task['id']).json()
            if job['status'] in {'succeeded', 'failed'}: break
            time.sleep(.01)
        assert job['status'] == 'succeeded'
        document = client.get('/api/km/v1/documents/' + job['document_id']).json()
        with runtime.session() as db:
            db.add(KnowledgeDiscoverySuggestion(id='published', tenant_id='local', knowledge_base_id=base['id'],
                knowledge_base_version_id=document['knowledge_base_version_id'], document_id=document['id'],
                title='Historical external resource', suggestion_type='generic', status='confirmed', payload_json={'document_id': document['id']}))
            db.add(Publication(tenant_id='local', suggestion_id='published', idempotency_key='original-key', status='confirmed', target_reference={'id': 'external-resource'}))
            db.commit()
    configuration = Configuration(modules={'concepts': True, 'discovery': True, 'publication': True})
    report = convert(path, configuration, tmp_path / 'backups')
    assert report['ok'], report
    assert 'knowledge_concepts' in read_database(Path(report['backup']) / 'original.sqlite')
    with TestClient(create_app(Runtime('sqlite:///' + str(path), configuration=configuration))) as client:
        assert client.get('/api/km/v1/concepts').json()
        assert client.get('/api/km/v1/suggestions').json()[0]['status'] == 'confirmed'
        assert 'publish_discovery' not in client.get('/api/km/config').json()['operations']
        assert client.post('/api/km/v1/suggestions/published/publish').status_code == 404
    data = read_database(path)
    assert data['km_publications'][0]['idempotency_key'] == 'original-key'
    assert json_value(data['km_publications'][0]['reference']) == {'id': 'external-resource'}
    configuration = Configuration()
    second = convert(path, configuration, tmp_path / 'backups')
    assert second['ok']
    data = read_database(path)
    assert not {'km_concepts', 'km_suggestions', 'km_publications'} & set(data)
    assert len(data['km_documents']) == report['counts']['documents']


def json_value(value):
    import json
    return json.loads(value) if isinstance(value, str) else value
