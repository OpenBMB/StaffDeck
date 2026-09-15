from contextlib import contextmanager
from types import SimpleNamespace as NS
import pytest
from fastapi import HTTPException

from app.api import memories, sessions, feedback
from app.scheduled_tasks import service as scheduled
from staffdeck_harness.runtime import staff_directory
from staffdeck_harness.contracts.staff import StaffProfile
from staffdeck_harness.contracts.security import ResourceRef


@pytest.mark.parametrize('fn', [memories._can_view_all_memories, sessions._can_view_all_agent_sessions,
                               feedback._can_view_all_agent_feedback])
@pytest.mark.parametrize('status,expected', [(200, True), (403, False), (503, None)])
def test_history_permissions_use_staff_source_and_live_pep(monkeypatch, fn, status, expected):
    calls = []
    actor = NS(id='remote-owner', tenant_id='tenant', role='member')
    def resolve(*args, **kw):
        calls.append(kw)
        if status != 200:
            raise HTTPException(status, 'source/permission outcome')
        return NS(id='remote-staff')
    monkeypatch.setattr(staff_directory, 'staff_profile', resolve)
    db = NS(get=lambda *_: pytest.fail('must not query local staff'))
    if expected is None:
        with pytest.raises(HTTPException) as exc:
            fn(db, 'tenant', 'remote-staff', actor)
        assert exc.value.status_code == 503
    else:
        assert fn(db, 'tenant', 'remote-staff', actor) is expected
    assert calls == [{'user': actor, 'action': 'manage'}]


def test_scheduled_access_uses_source_use_permission(monkeypatch):
    calls = []
    profile = NS(id='remote-staff', is_overall=False)
    monkeypatch.setattr(staff_directory, 'staff_profile', lambda *a, **kw: calls.append(kw) or profile)
    actor = NS(id='owner', tenant_id='tenant')
    db = NS(get=lambda *_: pytest.fail('local staff query'))
    assert scheduled._ensure_agent_access(db, 'tenant', 'remote-staff', actor) is profile
    assert calls == [{'user': actor, 'action': 'use', 'active_only': True}]


def test_scheduled_background_keeps_admitted_data_services(monkeypatch):
    task, run = NS(id='task'), NS(id='run')
    db = NS(get=lambda model, identifier: task if identifier == 'task' else run)
    purposes, execution = [], []
    @contextmanager
    def session(identity, *, purpose):
        purposes.append(purpose)
        yield db
    monkeypatch.setattr(scheduled, 'Session', lambda *_: pytest.fail('must not reopen default database'))
    monkeypatch.setattr(scheduled, '_execute_prepared_scheduled_task', lambda *args, **kw: execution.append(args))
    scheduled._execute_prepared_scheduled_task_in_background('task', 'run', True, data_services=NS(session=session))
    assert purposes == ['scheduled-task'] and execution == [(db, task, run)]


def test_staff_names_use_one_batch_for_duplicate_members(monkeypatch):
    calls = []
    rows = [StaffProfile(key, 'tenant', key.upper(), 'active', ResourceRef('agent', key, 'tenant')) for key in ('a', 'b')]
    source = NS(profiles=lambda ctx, ids: calls.append(ids) or rows)
    monkeypatch.setattr(staff_directory, 'resolve_source', lambda *a: source)
    db = NS(info={'staffdeck_registry': object(), 'staffdeck_actor_id': 'owner'})
    assert staff_directory.staff_names(db, 'tenant', ['a', 'b', 'a']) == {'a': 'A', 'b': 'B'}
    assert calls == [['a', 'b']]


def test_staff_batch_rejects_cross_tenant_rows(monkeypatch):
    source = NS(profiles=lambda *a: [StaffProfile('a', 'foreign', 'A', 'active', ResourceRef('agent', 'a', 'foreign'))])
    monkeypatch.setattr(staff_directory, 'resolve_source', lambda *a: source)
    with pytest.raises(HTTPException) as exc:
        staff_directory.staff_names(NS(info={'staffdeck_registry': object()}), 'tenant', ['a'])
    assert exc.value.status_code == 503


def test_trace_names_use_only_safe_composition_metadata():
    from staffdeck_harness.runtime.trace_names import composition_trace_names
    from staffdeck_harness.contracts.staff import CapabilityBindingView
    cap = CapabilityBindingView('general_skill', 'g1', None, ResourceRef('general_skill', 'g1', 'tenant'),
        'Skill name', metadata={'slug': 'example', 'provider_config': {'secret': 'never-export'}})
    names = composition_trace_names(NS(sops=(), capabilities=(cap,)))
    assert names['tools']['general_skill.example'] == 'Skill name'
    assert 'never-export' not in str(names)


def test_channel_name_loaders_do_not_scan_a_local_catalog():
    from app.channels.feishu_trace import _load_skill_trace_names
    from app.channels.adapters.wecom import _load_wecom_progress_names
    db = NS(exec=lambda *_: pytest.fail('no local resource table scans'))
    assert _load_skill_trace_names(db, 'tenant') == ({}, {}, {})
    assert _load_wecom_progress_names(NS(tenant_id='tenant')) == ({}, {}, {})


def test_archived_staff_history_still_requires_permission_and_cannot_execute():
    from sqlmodel import SQLModel, Session, create_engine
    from app.db.models import Tenant, User, AgentProfile
    engine = create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        owner = User(id='owner', tenant_id='tenant', username='owner', password_hash='test')
        other = User(id='other', tenant_id='tenant', username='other', password_hash='test')
        db.add_all([Tenant(id='tenant', name='Test'), owner, other,
                    AgentProfile(id='staff', tenant_id='tenant', name='Archived', status='archived',
                                 metadata_json={'owner_user_id': 'owner'})])
        db.commit()
        assert staff_directory.can_manage_staff(db, 'tenant', 'staff', owner)
        assert not staff_directory.can_manage_staff(db, 'tenant', 'staff', other)
        with pytest.raises(HTTPException):
            staff_directory.staff_profile(db, 'tenant', 'staff', user=owner, action='use', active_only=True)
    engine.dispose()
