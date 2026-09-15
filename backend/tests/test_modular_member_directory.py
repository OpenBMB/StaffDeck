from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from app.db.models import Tenant, User
from staffdeck_harness.contracts.members import MemberRecord
from staffdeck_harness.runtime import control_auth, identity_directory as directory


@pytest.fixture
def db():
    engine = create_engine('sqlite://', poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id='tenant', name='Test'))
        session.commit()
        yield session
    engine.dispose()


def member(**kw):
    return MemberRecord(**{'id': 'remote', 'tenant_id': 'tenant', 'username': 'remote',
                           'display_name': 'Remote member', 'source': 'external_directory', **kw})


def install(monkeypatch, rows):
    monkeypatch.setattr(control_auth, 'provider', lambda: NS(member_identity_source='external_directory', resolve_members=lambda *_: rows))


def test_remote_member_without_login_is_resolved_and_projected_without_replacing_actor(db, monkeypatch):
    install(monkeypatch, [member()])
    db.info['staffdeck_control_subject'] = actor = object()
    assert db.get(User, 'remote') is None
    resolved = directory.require_internal_member(db, 'tenant', 'remote', materialize=True)
    assert resolved.id == 'remote' and resolved.source == 'external_directory'
    assert db.info['staffdeck_control_subject'] is actor
    assert db.get(User, 'remote').password_hash == 'external-authentication-only'


@pytest.mark.parametrize('rows,status,code', [([], 404, 'MEMBER_NOT_FOUND'),
    ([member(disabled=True)], 409, 'MEMBER_DISABLED'),
    ([member(tenant_id='foreign')], 503, 'MEMBER_DIRECTORY_INVALID'),
    ([member(), member()], 503, 'MEMBER_DIRECTORY_INVALID')])
def test_remote_directory_rejection_never_creates_a_projection(db, monkeypatch, rows, status, code):
    install(monkeypatch, rows)
    with pytest.raises(HTTPException) as exc:
        directory.require_internal_member(db, 'tenant', 'remote', materialize=True)
    assert (exc.value.status_code, exc.value.detail['code']) == (status, code)
    assert db.get(User, 'remote') is None


def test_missing_directory_does_not_fallback_to_local_shadow(db, monkeypatch):
    db.add(User(id='remote', tenant_id='tenant', username='shadow', password_hash='x'))
    db.commit()
    monkeypatch.setattr(control_auth, 'provider', lambda: object())
    with pytest.raises(HTTPException) as exc:
        directory.require_internal_member(db, 'tenant', 'remote')
    assert exc.value.status_code == 503


def test_existing_foreign_identity_cannot_be_overwritten(db, monkeypatch):
    db.add(User(id='remote', tenant_id='tenant', username='shadow', password_hash='x', source='web'))
    db.commit()
    install(monkeypatch, [member()])
    with pytest.raises(HTTPException) as exc:
        directory.require_internal_member(db, 'tenant', 'remote', materialize=True)
    assert exc.value.detail['code'] == 'MEMBER_IDENTITY_CONFLICT'
    assert db.get(User, 'remote').source == 'web'


def test_username_collision_is_a_typed_conflict_not_an_internal_error(db, monkeypatch):
    db.add(User(id='other', tenant_id='tenant', username='remote', password_hash='x', source='web'))
    db.commit()
    install(monkeypatch, [member()])
    with pytest.raises(HTTPException) as exc:
        directory.require_internal_member(db, 'tenant', 'remote', materialize=True)
    assert exc.value.status_code == 409 and exc.value.detail['code'] == 'MEMBER_IDENTITY_CONFLICT'
    assert db.get(User, 'remote') is None and db.get(User, 'other').source == 'web'


def test_display_cache_does_not_cache_authority(db, monkeypatch):
    rows = [member(source='base_identity')]
    calls = []
    monkeypatch.setattr(control_auth, 'provider', lambda: NS(member_identity_source='base_identity', resolve_members=lambda *a: calls.append(a) or rows))
    assert directory.user_names(db, 'tenant', ['remote']) == {'remote': 'Remote member'}
    directory.user_names(db, 'tenant', ['remote'])
    assert len(calls) == 1
    rows[:] = [member(source='base_identity', disabled=True)]
    with pytest.raises(HTTPException) as exc:
        directory.require_internal_member(db, 'tenant', 'remote')
    assert exc.value.detail['code'] == 'MEMBER_DISABLED' and len(calls) == 2


def test_local_guest_is_not_an_internal_member(db, monkeypatch):
    monkeypatch.setattr(control_auth, 'provider', lambda: None)
    db.add(User(id='guest', tenant_id='tenant', username='guest', password_hash='x', source='feishu'))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        directory.require_internal_member(db, 'tenant', 'guest')
    assert exc.value.detail['code'] == 'MEMBER_NOT_FOUND'


def test_sop_handoff_accepts_external_member(db, monkeypatch):
    from app.api.skills import _validate_handoff_assignees
    install(monkeypatch, [member()])
    card = NS(nodes=[NS(assignee_user_id='remote', assignee_notify_channel='web')])
    _validate_handoff_assignees(db, card, 'tenant')
    assert db.get(User, 'remote').source == 'external_directory'


@pytest.mark.parametrize('operation', ['invite', 'manager', 'handoff'])
def test_all_channel_target_checks_use_directory(db, monkeypatch, operation):
    from app.api import channels
    from app.channels.schema import ChannelIdentityBindCodeCreate, ChannelBindingManagerCreate, ChannelBindingAgentsUpdate
    from app.db.models import ChannelBinding
    install(monkeypatch, [member()])
    admin = User(id='admin', tenant_id='tenant', username='admin', password_hash='x', role='admin')
    binding = ChannelBinding(id='channel', tenant_id='tenant', channel='feishu', credentials_enc='not-used',
                             agent_id='staff', created_by_user_id='admin')
    db.add_all([admin, binding]); db.commit()
    monkeypatch.setattr(channels, '_ensure_binding_manager', lambda *a, **kw: None)
    monkeypatch.setattr(channels, 'channel_binding_read', lambda *_: binding)
    if operation == 'invite':
        result = channels.create_identity_bind_code('channel', ChannelIdentityBindCodeCreate(user_id='remote'), 'tenant', admin, db)
        assert result.code
    elif operation == 'manager':
        result = channels.add_channel_binding_manager('channel', ChannelBindingManagerCreate(user_id='remote'), 'tenant', admin, db)
        assert result.user_id == 'remote'
    else:
        channels.update_channel_binding_agents('channel', ChannelBindingAgentsUpdate(default_handoff_assignee_user_id='remote',
                                              default_handoff_assignee_channel='web'), 'tenant', admin, db)
        assert binding.config_json['default_handoff_assignee_user_id'] == 'remote'
