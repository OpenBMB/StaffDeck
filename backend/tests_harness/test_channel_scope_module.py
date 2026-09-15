from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from app.channels.execution_scope import scope_is_current
from app.db.models import User, ChannelBinding, ChannelIdentity, ChannelInboundEvent, ChannelBindingAgent, ChatSession
from staffdeck_harness.contracts.runtime_services import ChannelExecutionScope


@pytest.fixture
def scoped_channel():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.info['staffdeck_runtime_services'] = NS(namespace='business-test')
        db.add(User(id='guest', tenant_id='t', username='guest', password_hash='unused', source='feishu'))
        db.add(ChannelBinding(id='binding', tenant_id='t', agent_id='remote', channel='feishu', status='active',
            external_account_key='feishu:app:test', config_revision=1))
        db.add(ChannelIdentity(id='mapping', tenant_id='t', channel='feishu', external_user_id='ou-guest', staffdeck_user_id='guest'))
        db.add(ChannelInboundEvent(id='event', tenant_id='t', binding_id='binding', channel='feishu', event_id='provider-event', status='processing',
            payload_json={'_staffdeck_actor': {'actor_id':'guest','event_id':'event','identity_id':'mapping','session_id':'chat'}}))
        db.add(ChannelBindingAgent(id='mount', tenant_id='t', binding_id='binding', agent_id='remote', is_default=True))
        db.add(ChatSession(id='chat', tenant_id='t', user_id='guest', agent_id='remote', channel='feishu',
            channel_binding_id='binding', channel_account_key='feishu:app:test', context_state_json={
                'channel_ingress_receipt': {'actor_id': 'guest', 'event_id': 'event', 'identity_id': 'mapping'}}))
        db.commit()
        scope = ChannelExecutionScope('t', 'guest', 'remote', 'chat', 'binding', 1, 'feishu:app:test', 'event', 'business-test')
        yield db, scope
    engine.dispose()


def test_guest_scope_uses_live_channel_mount_not_local_staff_row(scoped_channel):
    db, scope = scoped_channel
    assert scope_is_current(db, scope)


@pytest.mark.parametrize('change', [dict(tenant_id='other'), dict(actor_id='admin'), dict(agent_id='other'),
    dict(session_id='other'), dict(binding_revision=2), dict(account_key='another-app'), dict(namespace='oss-local')])
def test_guest_scope_rejects_cross_boundary_substitution(scoped_channel, change):
    db, scope = scoped_channel
    assert not scope_is_current(db, replace(scope, **change))


@pytest.mark.parametrize('mutation', ['disable', 'unmount', 'rebind', 'delete_actor', 'failed_ingress'])
def test_scope_revocation_is_effective_without_waiting_for_token_expiry(scoped_channel, mutation):
    db, scope = scoped_channel
    if mutation == 'disable':
        row = db.get(ChannelBinding, 'binding'); row.status = 'disabled'
    elif mutation == 'unmount':
        row = db.get(ChannelBindingAgent, 'mount'); row.agent_id = 'another-staff'
    elif mutation == 'rebind':
        row = db.get(ChannelIdentity, 'mapping'); row.staffdeck_user_id = 'base-user'
    elif mutation == 'delete_actor':
        row = db.get(User, 'guest'); db.delete(row); db.commit()
        assert not scope_is_current(db, scope)
        return
    else:
        row = db.get(ChannelInboundEvent, 'event'); row.status = 'failed'
    db.add(row); db.commit()
    assert not scope_is_current(db, scope)


def test_team_child_scope_uses_same_guest_without_exposing_other_teams(scoped_channel):
    from app.db.models import Team, TeamMember
    from app.teams.wakeup import _stamp_team_session
    from staffdeck_harness.contracts.runtime_services import ActorIdentity
    db, scope = scoped_channel
    binding = db.get(ChannelBinding, 'binding'); binding.team_id = 'team'
    root = db.get(ChatSession, 'chat'); root.team_id = 'team'
    db.add(binding); db.add(root)
    db.add(Team(id='team', tenant_id='t', name='Team', owner_user_id='owner'))
    db.add(TeamMember(id='leader', team_id='team', agent_id='remote', role='leader'))
    db.add(TeamMember(id='member', team_id='team', agent_id='remote-member', role='member'))
    db.info['staffdeck_control_subject'] = ActorIdentity('guest', 't', 'guest', 'Guest', 'member', 'channel_guest', scope)
    child = ChatSession(id='child', tenant_id='t', user_id='guest', agent_id='remote-member', team_id='team')
    db.add(child)
    _stamp_team_session(db, child)
    db.commit()
    child_scope = db.info['staffdeck_control_subject'].channel_scope
    assert child_scope.actor_id == 'guest' and child_scope.agent_id == 'remote-member'
    assert scope_is_current(db, child_scope)
    member = db.get(TeamMember, 'member'); db.delete(member); db.commit()
    assert not scope_is_current(db, child_scope)
