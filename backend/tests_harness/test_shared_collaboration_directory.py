from types import SimpleNamespace as NS
import pytest
from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select
from sqlalchemy.pool import StaticPool
from app.db.models import User, Team, AgentProfile, ChannelBinding
from app.api.channels import create_channel_binding
from app.channels.schema import ChannelBindingCreate
from app.teams.service import add_member
from app.teams.wakeup import build_team_planner_context
from staffdeck_harness.contracts.staff import StaffProfile
from staffdeck_harness.contracts.security import ResourceRef, SecurityContext
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.runtime import staff_directory


@pytest.fixture
def directory_db(monkeypatch):
    from staffdeck_harness.modules import registry
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user=User(id='operator',tenant_id='tenant',username='operator',password_hash='unused',role='admin')
        db.add(user);db.commit()
        db.info['staffdeck_actor_id']=user.id
        calls=[]
        class Source:
            def reference(self, context):
                return ResourceRef('agent',context.staff_id,context.tenant_id,{'owner_user_id':user.id})
            def profile(self, context):
                calls.append(context.staff_id)
                return StaffProfile(context.staff_id,context.tenant_id,'Remote '+context.staff_id,'active',
                    ResourceRef('agent',context.staff_id,context.tenant_id,{'owner_user_id':user.id}))
        identity=NS(resolve=lambda context,provider:SecurityContext(context.user_id,context.tenant_id,tenant_role='admin'))
        monkeypatch.setattr(registry,'peek_registry',lambda:NS())
        monkeypatch.setattr(staff_directory,'resolve_source',lambda reg,slot,services:Source() if slot==SlotName.STAFF_SOURCE else identity)
        yield db,user,calls
    engine.dispose()


def test_create_channel_uses_selected_staff_directory_without_local_shadow(directory_db):
    db,user,calls=directory_db
    result=create_channel_binding(ChannelBindingCreate(tenant_id='tenant',agent_id='remote-staff',channel='feishu',name='test'),user,db)
    assert result.agent_id=='remote-staff' and db.get(AgentProfile,'remote-staff') is None
    assert db.exec(select(ChannelBinding)).one().agent_id=='remote-staff'
    assert 'remote-staff' in calls


def test_team_roster_uses_the_same_directory(directory_db):
    db,user,calls=directory_db
    team=Team(id='team',tenant_id='tenant',name='Team',owner_user_id=user.id)
    db.add(team);db.commit()
    add_member(db,team,agent_id='remote-staff',role='leader')
    add_member(db,team,agent_id='remote-member')
    context=build_team_planner_context(db,team)
    assert {member.agent_id for member in context.members}=={'remote-staff','remote-member'}
    assert not db.exec(select(AgentProfile)).all()


def test_source_cannot_substitute_another_tenant(directory_db,monkeypatch):
    db,user,_=directory_db
    source=NS(profile=lambda context:StaffProfile('remote-staff','other','Wrong','active',ResourceRef('agent','remote-staff','other')))
    monkeypatch.setattr(staff_directory,'resolve_source',lambda *args:source)
    with pytest.raises(HTTPException) as error:
        staff_directory.staff_profile(db,'tenant','remote-staff')
    assert error.value.status_code==403


def test_staff_permission_denied_before_remote_profile_is_loaded(directory_db, monkeypatch):
    from staffdeck_harness.security import profile
    from staffdeck_harness.contracts.security import Decision
    db, user, calls = directory_db
    active = profile.get_profile(__import__('app.config', fromlist=['get_settings']).get_settings())
    monkeypatch.setattr(profile, 'get_profile', lambda *a: NS(
        name=active.name, identity=active.identity, pep=NS(authorize=lambda *a: Decision.deny('revoked'))))
    with pytest.raises(HTTPException) as error:
        staff_directory.ensure_staff_manager(db, 'tenant', 'remote-staff', user)
    assert error.value.status_code == 403
    assert calls == []


def test_channel_auto_route_candidates_use_shared_source(directory_db):
    from app.channels.service_autoroute import _route_candidates
    db, _, _ = directory_db
    assert [row['agent_id'] for row in _route_candidates(db, 'tenant', ['remote-a', 'remote-b'])] == ['remote-a', 'remote-b']
    assert not db.exec(select(AgentProfile)).all()


def test_resolving_team_member_never_inherits_another_members_workload():
    from staffdeck_harness.composition.sources import resolve_source
    from staffdeck_harness.contracts.sources import SourceContext
    from staffdeck_harness.contracts.runtime_services import ExecutionIdentity
    from staffdeck_harness.contracts.errors import PermissionDenied
    identity = ExecutionIdentity('tenant', 'actor', 'member-a', 'session', 'run', 1, 'trace')
    calls = []
    def resolve(context):
        calls.append(context)
        return context
    source = NS(reference=resolve, resolve=resolve, model=resolve, profile=resolve)
    item = NS(provider=NS(build=lambda db: source), enabled=True, slot=SlotName.STAFF_SOURCE,
              manifest=NS(module_id='remote-source'))
    registry = NS(provider=lambda slot: item)
    db = NS(info={'staffdeck_execution': identity})
    bound = resolve_source(registry, SlotName.STAFF_SOURCE, db)
    bound.profile(SourceContext('tenant', 'member-b', user_id='actor'))
    assert calls[-1].execution is None
    bound.profile(SourceContext('tenant', 'member-a', user_id='actor'))
    assert calls[-1].execution is identity
    with pytest.raises(PermissionDenied):
        bound.profile(SourceContext('tenant', 'member-b', user_id='actor', execution=identity))
