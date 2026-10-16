from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import os
import signal
import subprocess
import sys
import base64
import time

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from app.db.models import AgentProfile, AgentResourceBinding, PilotDeckProjectBinding, Tenant, User
from app.public_api.auth import PublicPrincipal
from app.public_api.errors import PublicAPIError
from app.public_api.project_bindings import EnsureProjectBinding, ensure_project_binding
from app.api.auth import AccountAPICredentialCreateRequest, create_account_api_credential
from app.public_api.app import create_public_api_app
from app.db import get_session
from fastapi.testclient import TestClient


def test_durable_binding_replay_concurrency_and_archive(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'binding.sqlite'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(id="actor", tenant_id="tenant", username="actor", role="admin", password_hash="unused")
        db.add(Tenant(id="tenant", name="Tenant"))
        db.add(user)
        db.commit()
    body = EnsureProjectBinding(installation_id=uuid4(), project_binding_id=uuid4(), name="Project A")
    def create():
        with Session(engine) as db:
            principal = PublicPrincipal("tenant", db.get(User, "actor"), frozenset({"agents:write"}))
            return ensure_project_binding(body, principal, db)["agent"]["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: create(), range(4)))
    assert len(set(ids)) == 1
    with Session(engine) as db:
        principal = PublicPrincipal("tenant", db.get(User, "actor"), frozenset({"agents:write"}))
        assert len(db.exec(select(AgentProfile)).all()) == 1
        assert len(db.exec(select(PilotDeckProjectBinding)).all()) == 1
        assert db.exec(select(AgentResourceBinding)).all() == []
        assert ensure_project_binding(body, principal, db)["agent"]["id"] == ids[0]
        agent = db.get(AgentProfile, ids[0])
        agent.status = "archived"
        db.add(agent)
        db.commit()
        with pytest.raises(PublicAPIError, match="archived"):
            ensure_project_binding(body, principal, db)
        assert len(db.exec(select(AgentProfile)).all()) == 1
    engine.dispose()


@pytest.mark.parametrize('phase', ['before_commit', 'after_commit'])
def test_process_killed_at_transaction_boundary_recovers_without_orphans(tmp_path, phase):
    url = f"sqlite:///{tmp_path / 'crash.sqlite'}"
    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id='tenant', name='Tenant'))
        db.add(User(id='actor', tenant_id='tenant', username='actor', role='admin', password_hash='unused'))
        db.commit()
    installation, binding = str(uuid4()), str(uuid4())
    script = '''
import os, signal, sys
from sqlalchemy import event
from sqlmodel import Session, create_engine
from app.db.models import User
from app.public_api.auth import PublicPrincipal
from app.public_api.project_bindings import EnsureProjectBinding, ensure_project_binding
url, installation, binding, phase = sys.argv[1:]
with Session(create_engine(url)) as db:
    principal = PublicPrincipal('tenant', db.get(User, 'actor'), frozenset({'agents:write'}))
    def interrupt(session):
        if phase == 'before_commit':
            session.flush()
        os.kill(os.getpid(), signal.SIGKILL)
    event.listen(db, phase, interrupt)
    ensure_project_binding(EnsureProjectBinding(installation_id=installation,
        project_binding_id=binding, name='Crash recovery'), principal, db)
'''
    child = subprocess.run([sys.executable, '-c', script, url, installation, binding, phase],
                           env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert child.returncode == -signal.SIGKILL, child.stderr
    with Session(engine) as db:
        expected = 0 if phase == 'before_commit' else 1
        assert len(db.exec(select(AgentProfile)).all()) == expected
        assert len(db.exec(select(PilotDeckProjectBinding)).all()) == expected
        principal = PublicPrincipal('tenant', db.get(User, 'actor'), frozenset({'agents:write'}))
        body = EnsureProjectBinding(installation_id=installation, project_binding_id=binding, name='Crash recovery')
        target = ensure_project_binding(body, principal, db)['agent']['id']
        assert ensure_project_binding(body, principal, db)['agent']['id'] == target
        assert len(db.exec(select(AgentProfile)).all()) == 1
        assert len(db.exec(select(PilotDeckProjectBinding)).all()) == 1
    engine.dispose()


def test_public_binding_api_and_project_resource_isolation(tmp_path, independent_knowledge_service):
    km = independent_knowledge_service('tenant')
    engine = create_engine(f"sqlite:///{tmp_path / 'api.sqlite'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(id="actor", tenant_id="tenant", username="actor", role="admin", password_hash="unused")
        db.add(Tenant(id="tenant", name="Tenant"))
        db.add(user)
        db.commit()
        credential = create_account_api_credential(AccountAPICredentialCreateRequest(name="PD"), user, db)
    app = create_public_api_app()
    def session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_session] = session
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {credential.api_key}"}
    installation = str(uuid4())
    def ensure(name):
        body = {"installation_id": installation, "project_binding_id": str(uuid4()), "name": name}
        assert client.post('/agents:ensure-project-binding', json=body).status_code == 401
        first = client.post('/agents:ensure-project-binding', json=body, headers=headers)
        assert first.status_code == 200, first.text
        assert client.post('/agents:ensure-project-binding', json=body, headers=headers).json() == first.json()
        return first.json()['agent']['id']
    a, b = ensure('Project A'), ensure('Project B')
    def module(agent, operation, value):
        return client.post(f'/agents/{agent}/knowledge-management/v2/module/call', headers=headers,
            json={'kind': 'request', 'method': 'module_call', 'messageId': 'm', 'runId': 'r',
                  'operationId': 'o', 'requestId': 'q', 'module': 'knowledge',
                  'payload': {'operation': operation, 'input': value}})
    for scope in ['general', 'sop_specific']:
        created = module(a, 'create_base', {'name': scope, 'capabilityScope': scope})
        assert created.status_code == 200, created.text
        base = created.json()['payload']['result']
        assert base['capability_scope'] == scope
        denied = module(b, 'get_base', {'knowledgeBaseId': base['id']})
        assert denied.status_code == 404, denied.text
    assert len(module(a, 'list_bases', {}).json()['payload']['result']) == 2
    assert module(b, 'list_bases', {}).json()['payload']['result'] == []
    assert module(a, 'list_bases', {'agentId': b}).status_code == 403
    base_id = module(a, 'list_bases', {}).json()['payload']['result'][0]['id']
    uploaded = module(a, 'import_document', {'knowledgeBaseId': base_id, 'filename': 'a.md',
        'contentBase64': base64.b64encode(b'# A\n\nA private marker').decode()})
    assert uploaded.status_code == 200, uploaded.text
    job = uploaded.json()['payload']['result']
    for _ in range(100):
        result = module(a, 'get_job', {'jobId': job['id']}).json()['payload']['result']
        if result['status'] in {'succeeded', 'failed'}:
            break
        time.sleep(.02)
    assert result['status'] == 'succeeded', result
    document_id = result['document_id']
    bucket = module(a, 'list_document_buckets', {'documentId': document_id}).json()['payload']['result'][0]
    chunk = module(a, 'list_bucket_chunks', {'bucketId': bucket['id']}).json()['payload']['result'][0]
    for operation, value in [
        ('get_document', {'documentId': document_id}),
        ('update_document', {'documentId': document_id, 'content': 'changed'}),
        ('delete_document', {'documentId': document_id}),
        ('list_document_buckets', {'documentId': document_id}),
        ('update_bucket', {'bucketId': bucket['id'], 'summary': 'changed'}),
        ('list_bucket_chunks', {'bucketId': bucket['id']}),
        ('update_chunk', {'chunkId': chunk['id'], 'content': 'changed'}),
        ('resolve_citation', {'chunkId': chunk['id']}),
        ('get_job', {'jobId': job['id']}),
        ('cancel_job', {'jobId': job['id']}),
        ('confirm_discovery', {'suggestionId': 'suggestion-a'}),
        ('reject_discovery', {'suggestionId': 'suggestion-a'}),
        ('list_versions', {'knowledgeBaseId': base_id}),
        ('publish_version', {'knowledgeBaseId': base_id}),
        ('query', {'query': 'marker', 'knowledgeBaseIds': [base_id]}),
    ]:
        denied = module(b, operation, value)
        assert denied.status_code == 404, (operation, denied.text)
    assert module(a, 'resolve_citation', {'chunkId': chunk['id']}).status_code == 200
    card = {'skill_id': 'project_a_sop', 'name': 'Project A SOP', 'version': '1.0.0',
            'description': 'A only', 'trigger_intents': ['A'],
            'nodes': [{'node_id': 'answer', 'type': 'respond', 'name': 'Answer', 'instruction': 'Answer A',
                       'capability_refs': {'general_skill_ids': [], 'tool_ids': [], 'knowledge_base_ids': []}}],
            'edges': [], 'start_node_id': 'answer', 'terminal_node_ids': ['answer']}
    draft = client.post(f'/agents/{a}/sops', headers=headers, json={'content': card})
    assert draft.status_code == 201, draft.text
    published = client.post(f'/sops/project_a_sop:publish?agent_id={a}', headers=headers,
                            json={'draft_id': draft.json()['id']})
    assert published.status_code == 200, published.text
    versions = client.get('/sops/project_a_sop/versions?agent_id=' + a, headers=headers)
    version = versions.json()['data'][0]
    assert version['status'] == 'active'
    edited = client.post(f'/agents/{a}/sops', headers=headers,
                        json={'content': version['content'], 'base_version': version['version']})
    assert edited.status_code == 201, edited.text
    assert len(client.get(f'/agents/{a}/sops', headers=headers).json()['data']) == 1
    assert client.get(f'/agents/{b}/sops', headers=headers).json()['data'] == []
    with Session(engine) as db:
        principal = PublicPrincipal('tenant', db.get(User, 'actor'), frozenset({'agents:write'}), agent_id=a)
        with pytest.raises(PublicAPIError, match='tenant key'):
            ensure_project_binding(EnsureProjectBinding(installation_id=uuid4(), project_binding_id=uuid4(), name='denied'), principal, db)
        assert len(db.exec(select(AgentProfile)).all()) == 2
    client.close()
    engine.dispose()


def test_binding_contract_regular_owner_tenant_isolation_and_atomic_failure(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'contract.sqlite'}",
                           connect_args={'check_same_thread': False})
    SQLModel.metadata.create_all(engine)
    credentials = {}
    with Session(engine) as db:
        for tenant in ['tenant', 'other']:
            db.add(Tenant(id=tenant, name=tenant))
        for name, tenant in [('actor', 'tenant'), ('stranger', 'tenant'), ('other', 'other')]:
            user = User(id=name, tenant_id=tenant, username=name, role='member', password_hash='unused')
            db.add(user)
            db.commit()
            credentials[name] = create_account_api_credential(
                AccountAPICredentialCreateRequest(name='binding'), user, db).api_key
    app = create_public_api_app()
    def session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_session] = session
    with TestClient(app) as client:
        body = {'installation_id': str(uuid4()), 'project_binding_id': str(uuid4()), 'name': 'Original'}
        headers = {'Authorization': f'Bearer {credentials["actor"]}'}
        response = client.post('/agents:ensure-project-binding', headers=headers, json=body)
        assert response.status_code == 200, response.text
        target = response.json()['agent']['id']
        replay = client.post('/agents:ensure-project-binding', headers=headers,
                             json={**body, 'name': 'Renamed'})
        assert replay.json()['agent']['id'] == target
        assert replay.json()['agent']['name'] == 'Original'
        for changes in [{'installation_id': 'bad'}, {'project_binding_id': 'bad'},
                        {'name': ''}, {'name': 'x' * 121}, {'source_mode': 'copy'}]:
            assert client.post('/agents:ensure-project-binding', headers=headers,
                               json={**body, **changes}).status_code == 422
        assert client.post('/agents:ensure-project-binding',
            headers={'Authorization': f'Bearer {credentials["stranger"]}'}, json=body).status_code == 404
        other = client.post('/agents:ensure-project-binding',
            headers={'Authorization': f'Bearer {credentials["other"]}'}, json=body)
        assert other.status_code == 200
        assert other.json()['agent']['id'] != target
    with Session(engine) as db:
        actor = db.get(User, 'actor')
        principal = PublicPrincipal('tenant', actor, frozenset({'agents:write'}))
        original = db.commit
        def failed_commit():
            raise RuntimeError('injected transaction failure')
        monkeypatch.setattr(db, 'commit', failed_commit)
        with pytest.raises(RuntimeError, match='transaction failure'):
            ensure_project_binding(EnsureProjectBinding(installation_id=uuid4(),
                project_binding_id=uuid4(), name='Rollback'), principal, db)
        db.rollback()
        monkeypatch.setattr(db, 'commit', original)
        assert len(db.exec(select(AgentProfile)).all()) == 2
        assert len(db.exec(select(PilotDeckProjectBinding)).all()) == 2
    engine.dispose()
