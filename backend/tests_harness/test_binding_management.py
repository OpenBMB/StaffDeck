from contextlib import nullcontext
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from app.db.models import AgentProfile, AgentResourceBinding, Tenant, User
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.runtime.resource_bindings import LocalEmployeeBindings, selected_bindings


def test_binding_management_resolves_selected_source_without_edition_branch():
    manager = NS(list=lambda *a: [], require=lambda *a: {}, change=lambda *a: {})
    calls = []
    def build(db, request, agent_id):
        calls.append(agent_id)
        return nullcontext(manager)
    registry = NS(provider=lambda slot: NS(provider=NS(binding_manager=build)) if slot == SlotName.STAFF_SOURCE else None)
    with selected_bindings(NS(info={'staffdeck_registry': registry}), NS(), 'external-staff') as result:
        assert result is manager
    assert calls == ['external-staff']


def test_local_binding_lifecycle_does_not_require_a_local_copy_of_remote_resource():
    engine = create_engine('sqlite://', poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Tenant(id='t', name='tenant'),
            User(id='u', tenant_id='t', username='u', password_hash='x', role='admin'),
            AgentProfile(id='a', tenant_id='t', name='a', metadata_json={'owner_user_id': 'u'}),
            AgentResourceBinding(id='binding', tenant_id='t', agent_id='a', resource_type='knowledge_base', resource_id='remote-kb'),
            AgentResourceBinding(id='other', tenant_id='t', agent_id='a', resource_type='tool', resource_id='remote-tool')])
        db.commit()
        manager = LocalEmployeeBindings(db, NS(subject=NS(tenant_id='t', user_id='u')), 'a')
        manager.change('knowledge_base', 'remote-kb', active=False)
        assert manager.require('knowledge_base', 'remote-kb')['status'] == 'inactive'
        assert manager.require('tool', 'remote-tool')['status'] == 'active'
        manager.change('knowledge_base', 'remote-kb', active=True)
        assert manager.require('knowledge_base', 'remote-kb')['status'] == 'active'
        manager.change('knowledge_base', 'remote-kb', active=False, remove=True)
        assert len(db.exec(select(AgentResourceBinding)).all()) == 1
