"""The shared team worker must retain both data realm and real requesting actor."""
from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from app.db.models import User, Team, TeamWakeEvent
from app.teams import wakeup
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.runtime import actors, control_auth, session_binding


@pytest.fixture
def runtime_db(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(control_auth, 'provider', lambda: None)
    monkeypatch.setattr(session_binding, 'current_binding', lambda db: db.info.get('test_binding'))
    with Session(engine) as db:
        db.add(User(id='requester', tenant_id='t', username='requester', password_hash='unused'))
        db.add(Team(id='team', tenant_id='t', name='Team', owner_user_id='owner'))
        db.commit()
        yield db, engine
    engine.dispose()


def test_enqueued_event_uses_requester_not_team_owner_or_payload(runtime_db):
    db, _ = runtime_db
    db.info.update(staffdeck_actor_id='requester', test_binding={'namespace': 'business'})
    event = wakeup.enqueue_wake_event(db, team=db.get(Team, 'team'), target_agent_id='remote',
        trigger_type='manual', payload={'_runtime_context': {'actor_id': 'owner'}})
    assert event.payload_json['_runtime_context'] == {
        'actor_id': 'requester', 'binding': {'namespace': 'business'}}


@pytest.mark.parametrize('captured', [None, {'actor_id': 'requester', 'binding': {'namespace': 'other'}},
                                    {'actor_id': 'deleted', 'binding': {'namespace': 'business'}}])
def test_missing_or_foreign_execution_identity_fails_closed(runtime_db, captured):
    db, _ = runtime_db
    db.info['test_binding'] = {'namespace': 'business'}
    with pytest.raises(PermissionDenied):
        actors.restore_actor(db, 't', captured)


def test_worker_uses_selected_factory_and_claims_only_once(runtime_db, monkeypatch):
    db, engine = runtime_db
    event = TeamWakeEvent(id='wake', tenant_id='t', team_id='team', target_agent_id='remote',
        trigger_type='manual', payload_json={'_runtime_context': {'actor_id': 'requester', 'binding': None}})
    db.add(event)
    db.commit()
    calls = []
    @contextmanager
    def session(identity, *, purpose):
        calls.append(purpose)
        with Session(engine) as worker:
            yield worker
    def verify(db, actor):
        calls.append(actor.id)
        return actor
    def execute(db, event):
        calls.append('execute')
        event.status = 'done'
        db.add(event)
        db.commit()
    monkeypatch.setattr(actors, 'bind_actor', verify)
    monkeypatch.setattr(wakeup, 'execute_wake_event', execute)
    services = NS(session=session)
    wakeup._execute_wakeup_in_background('wake', data_services=services)
    wakeup._execute_wakeup_in_background('wake', data_services=services)
    assert calls == ['team-wake', 'requester', 'execute', 'team-wake']


def test_revoked_actor_does_not_run_team_task(runtime_db, monkeypatch):
    db, engine = runtime_db
    db.add(TeamWakeEvent(id='revoked', tenant_id='t', team_id='team', target_agent_id='remote',
        trigger_type='manual', payload_json={'_runtime_context': {'actor_id': 'requester', 'binding': None}}))
    db.commit()
    @contextmanager
    def session(identity, *, purpose):
        with Session(engine) as worker:
            yield worker
    def denied(*a):
        raise PermissionDenied('revoked')
    monkeypatch.setattr(actors, 'bind_actor', denied)
    monkeypatch.setattr(wakeup, 'execute_wake_event', lambda *a: pytest.fail('revoked user ran task'))
    wakeup._execute_wakeup_in_background('revoked', data_services=NS(session=session))
    db.expire_all()
    assert db.get(TeamWakeEvent, 'revoked').status == 'failed'


def test_enterprise_task_without_requester_never_borrows_team_owner(runtime_db, monkeypatch):
    db, _ = runtime_db
    monkeypatch.setattr(control_auth, 'provider', lambda: object())
    with pytest.raises(PermissionDenied):
        actors.capture_actor(db, legacy_owner='owner')
