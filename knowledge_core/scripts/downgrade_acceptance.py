"""Offline merge, backup, failure recovery and post-write restore rehearsal."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch
from fastapi.testclient import TestClient
from knowledge_core.api import create_app
from knowledge_core.configuration import Configuration
from knowledge_core.conversion import convert, read_database, restore, recover_interrupted, export_changes, preflight
from knowledge_core.identity import Principal
from knowledge_core.kernel import storage as s
from knowledge_core.runtime import Runtime


def require(condition, message):
    if not condition: raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    root = args.output_dir.resolve(); root.mkdir(parents=True, exist_ok=True)
    path = root / 'knowledge.sqlite'
    if path.exists(): raise ValueError('Use a fresh rehearsal directory')
    settings = Configuration(modules={'spaces': True, 'catalog': True, 'versions': True, 'concepts': True, 'discovery': True})
    config = root / 'configuration.json'; config.write_text(json.dumps(settings.as_dict()))
    runtime = Runtime('sqlite:///' + str(path), configuration=settings)
    runtime.initialize()
    scopes = [{'tenant_id': 'local', 'space_id': space, 'knowledge_base_id': base} for space, base in
              (('a', 'one'), ('b', 'one'), ('b', 'two'))]
    with runtime.engine.begin() as db:
        spaces, catalog, versions = (runtime.plugins[name] for name in ('spaces', 'catalog', 'versions'))
        for space in ('a', 'b'): db.execute(spaces.spaces.insert().values(id=space, name=space, tenant_id='local'))
        for base in ('one', 'two'):
            db.execute(catalog.catalogs.insert().values(id=base, name=base, description='', status='active', created_at=s.now(), updated_at=s.now()))
        for scope in scopes:
            db.execute(catalog.bindings.insert().values(space_id=scope['space_id'], knowledge_base_id=scope['knowledge_base_id']))
            versions.branch(db, scope, create=True)
    actor = Principal('local')
    runtime.content.write('original', 'same.md', '# Expenses\n\nExpense receipts from revision one.', 'Same title', {}, scopes[0], actor)
    runtime.content.write('second-base', 'same.md', '# Expenses\n\nExpense approvals for another knowledge base.', 'Same title', {}, scopes[2], actor)
    with runtime.engine.begin() as db:
        version = versions.checkpoint(db, scopes[0], 'Source snapshot')
        remote = versions.branch(db, scopes[1])
        copies = versions.restore(db, remote, version, scopes[1])
        clone = copies['document_mapping']['original']
        db.execute(runtime.plugins['discovery'].suggestions.insert().values(id='published', document_id='original', title='Published resource',
            suggestion_type='procedure', status='confirmed', payload={'external_reference': 'existing-host-resource'},
            source_refs=[], created_at=s.now(), updated_at=s.now()))
    row = runtime.content.get(s.documents, clone, scopes[1], actor)
    runtime.content.edit(s.documents, clone, {'expected_updated_at': row['updated_at'].isoformat(),
        'content_md': '# Expenses\n\nExpense receipts differ on branch two.'}, scopes[1], actor)
    with runtime.engine.begin() as db:
        version = versions.checkpoint(db, scopes[1], 'Different branch')
        # A second copy of precisely the same source revision must merge once.
        copies = versions.restore(db, versions.branch(db, scopes[1]), version, scopes[1])
        shared = copies['document_mapping'][clone]
        db.execute(versions.contents.insert().values(branch_id=versions.branch(db, scopes[0])['id'], document_id=clone))
    old = runtime.content.list(s.chunks, scopes[0], actor)[0]['id']
    original_state = read_database(path)
    runtime.close()
    source = preflight(path, Configuration())
    (root / 'preflight.json').write_text(json.dumps(source, indent=2))
    report = convert(path, Configuration(), root / 'backups', config)
    require(report['ok'] and report['counts']['documents'] == 3, 'Current revisions were lost or duplicated')
    require(report['document_mapping'][clone] == report['document_mapping'][shared], 'Shared revision was not deduplicated')
    (root / 'conversion.json').write_text(json.dumps(report, indent=2))
    backup = Path(report['backup'])
    data = read_database(path)
    require(set(data) == {*s.CORE_TABLES, 'km_conversion_guard'}, 'Disabled module tables remain')
    require(read_database(backup / 'original.sqlite') == original_state, 'Original backup differs')
    with TestClient(create_app(Runtime('sqlite:///' + str(path)))) as client:
        documents = client.get('/api/km/v1/documents').json()
        require(len(documents) == 3 and all(row['title'] == 'Same title' for row in documents), 'Titles or merged documents changed')
        require(client.get('/api/km/v1/citations/' + old).status_code == 200, 'Historical citation lost')
        query = client.post('/api/km/v1/query', json={'query': 'Expense receipts'}).json()
        require(query['evidence_pack'], 'Merged content retrieval failed')
        (root / 'readback.json').write_text(json.dumps({'documents': documents, 'query': query}, indent=2))
    require(restore(path, backup)['ok'], 'Pre-write restore failed')
    replace = os.replace
    def interrupted(source, target):
        if target == config: raise KeyboardInterrupt('Rehearsal interruption')
        return replace(source, target)
    try:
        with patch('knowledge_core.conversion.os.replace', interrupted): convert(path, Configuration(), root / 'backups', config)
    except KeyboardInterrupt: pass
    else: raise RuntimeError('Interruption did not execute')
    recovery = recover_interrupted(path)
    require(recovery['ok'] and Configuration.load(config).enabled('spaces'), 'Interrupted conversion recovery failed')
    (root / 'interruption.json').write_text(json.dumps(recovery, indent=2))
    report = convert(path, Configuration(), root / 'backups', config)
    with TestClient(create_app(Runtime('sqlite:///' + str(path)))) as client:
        document = client.get('/api/km/v1/documents').json()[0]
        response = client.put('/api/km/v1/documents/' + document['id'], json={'expected_updated_at': document['updated_at'], 'title': 'New write'})
        require(response.status_code == 200, 'Sample post-conversion write failed')
    try: restore(path, Path(report['backup']))
    except ValueError as error: require('New writes exist' in str(error), str(error))
    else: raise RuntimeError('Restore overwrote post-conversion writes')
    exported = export_changes(path, Path(report['backup']), root / 'new-content.json')
    require(exported['changed_documents'] == 1, 'Post-conversion export missing')
    checks = [
        {'name': 'merge_revisions_and_references', 'status': 'passed', 'evidence': [root.name + '/' + name for name in ('preflight.json', 'conversion.json', 'readback.json')]},
        {'name': 'persistent_backup_and_prewrite_restore', 'status': 'passed', 'evidence': [str((backup / 'report.json').relative_to(root.parent)), str((backup / 'original.sqlite').relative_to(root.parent))]},
        {'name': 'interruption_recovery', 'status': 'passed', 'evidence': [root.name + '/interruption.json']},
        {'name': 'postwrite_restore_guard_and_export', 'status': 'passed', 'evidence': [root.name + '/new-content.json']},
    ]
    result = {'status': 'passed', 'checks': checks}
    (root.parent / 'downgrade.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__': main()
