from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace as NS

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from staffdeck_harness.contracts.directory import DirectorySnapshot, select_directory
from staffdeck_harness.contracts.errors import ModuleSdkError


def snapshot():
    def entry(id, *, owner='other', public=False, shared=False, active=True, actions=('view', 'use')):
        return {'profile': {'id': id, 'tenant_id': 't', 'name': id, 'is_overall': False,
                'status': 'active' if active else 'archived', 'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z'},
                'owner_user_id': owner, 'public': public, 'shared': shared, 'allowed_actions': list(actions)}
    return DirectorySnapshot.model_validate({'tenant_id': 't', 'subject_user_id': 'u', 'entries': [
        entry('own-public', owner='u', public=True, actions=('view', 'use', 'manage')),
        entry('own-closed', owner='u', active=False, actions=('view', 'manage')),
        entry('public', public=True), entry('shared-editor', shared=True, actions=('view', 'use', 'manage')),
        entry('public-and-shared', public=True, shared=True), entry('revoked', actions=()),
    ]})


@pytest.mark.parametrize('scope,expected', [
    ('available', {'own-public', 'public', 'shared-editor', 'public-and-shared'}),
    ('mine', {'own-public', 'own-closed'}),
    ('gallery', {'own-public', 'public', 'public-and-shared'}),
    ('shared', {'shared-editor', 'public-and-shared'}),
    ('managed', {'own-public', 'own-closed', 'shared-editor'}),
])
def test_scope_is_one_shared_semantic_contract(scope, expected):
    assert {entry.profile['id'] for entry in select_directory(snapshot(), tenant_id='t', user_id='u', scope=scope)} == expected


def test_identity_scope_and_conflicting_duplicates_fail_closed():
    for tenant, user in [('other', 'u'), ('t', 'other')]:
        with pytest.raises(ModuleSdkError):
            select_directory(snapshot(), tenant_id=tenant, user_id=user, scope='gallery')
    value = snapshot()
    duplicate = value.entries[0].model_copy(deep=True)
    value.entries.append(duplicate)
    assert len(select_directory(value, tenant_id='t', user_id='u', scope='gallery')) == 3
    duplicate.public = False
    with pytest.raises(ModuleSdkError):
        select_directory(value, tenant_id='t', user_id='u', scope='gallery')


@pytest.mark.parametrize('adapter', ['local', 'business'])
def test_source_adapters_produce_the_same_directory_sets(monkeypatch, adapter):
    from app.db.models import Tenant, User, AgentProfile
    from staffdeck_harness.composition.local_sources import LocalStaffSource
    from staffdeck_harness.contracts.sources import SourceContext
    from staffdeck_harness.contracts.runtime_services import ActorIdentity
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id='t', name='Test'))
        db.add(User(id='u', tenant_id='t', username='user', role='member', password_hash='unused'))
        for id, owner, public, active in [('own', 'u', False, True), ('own-public', 'u', True, True),
            ('other-public', 'other', True, True), ('private', 'other', False, True), ('closed', 'u', False, False)]:
            db.add(AgentProfile(id=id, tenant_id='t', name=id, status='active' if active else 'archived',
                metadata_json={'owner_user_id': owner, 'published_to_gallery': public}))
        db.commit()
        context = SourceContext('t', None, user_id='u', subject=ActorIdentity('u', 't', 'user', 'User', 'member', 'base_identity'))
        payload = LocalStaffSource(db).directory(context)
        if adapter == 'business':
            BusinessStaffSource = pytest.importorskip('staffdeck_business_modules.sources').BusinessStaffSource
            BusinessSettings = pytest.importorskip('staffdeck_business_modules.configuration').BusinessSettings
            def handle(request):
                assert request.url.path == '/api/agent-control-plane/agents/directory-facts'
                return httpx.Response(200, json=payload)
            client = httpx.Client(transport=httpx.MockTransport(handle))
            monkeypatch.setattr(httpx, 'Client', lambda **kw: client)
            payload = BusinessStaffSource(BusinessSettings(tenant_id='t', gateway_url='http://test'), None).directory(context, authorization='Bearer unit')
        snap = DirectorySnapshot.model_validate(payload)
        for scope, expected in [('available', {'own', 'own-public', 'other-public'}),
            ('gallery', {'own-public', 'other-public'}), ('mine', {'own', 'own-public', 'closed'}), ('shared', set())]:
            assert {e.profile['id'] for e in select_directory(snap, tenant_id='t', user_id='u', scope=scope)} == expected


def test_statistics_outage_does_not_erase_directory_or_invent_zero(monkeypatch):
    from staffdeck_harness.runtime.directory import employee_directory
    from staffdeck_harness.composition.local_sources import LocalStaffSource
    from staffdeck_harness.modules import registry
    monkeypatch.setattr(registry, 'peek_registry', lambda: None)
    monkeypatch.setattr(LocalStaffSource, 'directory', lambda *a, **kw: snapshot())
    def fail(*args): raise OperationalError('statistics', {}, Exception('offline'))
    db = NS(info={}, exec=fail, rollback=lambda: None)
    result = employee_directory(db, 't', NS(id='u', tenant_id='t'), scope='gallery')
    assert len(result) == 3
    assert all(row.metadata['directory_statistics'] == {'status': 'unavailable', 'chat_count': None} for row in result)


def test_source_summary_preserves_counts_and_never_substitutes_for_details(monkeypatch):
    from fastapi import HTTPException
    from staffdeck_harness.runtime.directory import employee_directory
    from staffdeck_harness.composition.local_sources import LocalStaffSource
    from staffdeck_harness.modules import registry
    monkeypatch.setattr(registry, 'peek_registry', lambda: None)
    value = snapshot()
    value.projection = 'summary'
    for entry in value.entries:
        entry.profile['metadata'] = {'resource_counts': {'general_skill': 7}}
    monkeypatch.setattr(LocalStaffSource, 'directory', lambda *a, **kw: value)
    def fail(*args): raise OperationalError('statistics', {}, Exception('offline'))
    db = NS(info={}, exec=fail, rollback=lambda: None)
    rows = employee_directory(db, 't', NS(id='u', tenant_id='t'), scope='gallery', summary=True)
    assert all(row.metadata['resource_counts'] == {'general_skill': 7} for row in rows)
    with pytest.raises(HTTPException) as error:
        employee_directory(db, 't', NS(id='u', tenant_id='t'), scope='gallery')
    assert error.value.status_code == 503
    value.entries[0].profile['metadata'] = {}
    with pytest.raises(HTTPException):
        employee_directory(db, 't', NS(id='u', tenant_id='t'), scope='gallery', summary=True)
