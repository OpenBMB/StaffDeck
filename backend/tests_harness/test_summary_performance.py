import base64
from types import SimpleNamespace
import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.requests import Request
from app.db.models import AgentProfile, AgentResourceBinding, Tenant, Tool, User
from app.api.agents import _bindings_by_agent, _resource_binding_visible_in_agent_summary, list_agents, get_employee_avatar


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr('staffdeck_harness.modules.registry._active',None)
    engine=create_engine('sqlite://',poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Tenant(id='t',name='T'),User(id='u',tenant_id='t',username='u',password_hash='x',role='admin'),
            AgentProfile(id='a',tenant_id='t',name='A',metadata_json={'owner_user_id':'u','avatar_kind':'upload',
                'avatar_image':'data:image/png;base64,'+base64.b64encode(b'PNG-test-data').decode()})])
        for i in range(80):
            db.add(Tool(id=f'tool{i}',tenant_id='t',name=f'tool{i}',method='GET',url='http://unused',enabled=i%2==0))
            db.add(AgentResourceBinding(id=f'b{i}',tenant_id='t',agent_id='a',resource_type='tool',resource_id=f'tool{i}',metadata_json={'visibility':'agent_private'}))
        db.commit()
        yield db


def test_batch_summary_matches_existing_rules_with_bounded_query_count(db):
    actor=db.get(AgentProfile,'a')
    bindings=db.exec(select(AgentResourceBinding)).all()
    expected={b.id for b in bindings if _resource_binding_visible_in_agent_summary(db,'t',actor,b)}
    queries=[]
    def track(*args): queries.append(True)
    event.listen(db.get_bind(),'before_cursor_execute',track)
    try: result=_bindings_by_agent(db,'t')
    finally: event.remove(db.get_bind(),'before_cursor_execute',track)
    assert {b.id for b in result['a']}==expected
    assert len(queries)<=12


def test_summary_replaces_inline_avatar_and_preserves_counts_without_changing_details(db):
    user=db.get(User,'u')
    request=Request({'type':'http','headers':[],'query_string':b'view=summary'})
    summary=list_agents('t',db,user,request)[0]
    assert summary.resources==[]
    assert summary.metadata['resource_counts']['tool']==40
    assert 'avatar_image' not in summary.metadata
    path=summary.metadata['avatar_resource_url'].split('?')[0]
    digest=path.rsplit('/',1)[1]
    image=get_employee_avatar('a',digest,'t',Request({'type':'http','headers':[]}),db,user)
    assert image.body==b'PNG-test-data'
    assert image.headers['cache-control'].startswith('private')
    from staffdeck_harness.runtime import avatar_assets
    with avatar_assets._lock:
        avatar_assets._items.clear()
        avatar_assets._bytes=0
    assert get_employee_avatar('a',digest,'t',Request({'type':'http','headers':[]}),db,user).body==b'PNG-test-data'
    full=list_agents('t',db,user)[0]
    assert len(full.resources)==40 and full.metadata['avatar_image'].startswith('data:image/png')
    from fastapi import HTTPException
    other=User(id='other',tenant_id='t',username='other',password_hash='x',role='member')
    db.add(other);db.commit()
    with pytest.raises(HTTPException) as denied:
        get_employee_avatar('a',digest,'t',Request({'type':'http','headers':[]}),db,other)
    assert denied.value.status_code==403


def test_inactive_channels_do_not_require_base_channel_contract():
    from staffdeck_harness.modules.kernel import ChannelHostModule
    called=[]
    registry=SimpleNamespace(providers=lambda slot:[SimpleNamespace(manifest=SimpleNamespace(module_id='channel.host'),provider=SimpleNamespace())],
        provider=lambda slot:SimpleNamespace(provider=SimpleNamespace(preflight_channel=lambda:called.append(True))))
    ChannelHostModule(registry).preflight_module({})
    assert called==[]
    registry.providers=lambda slot:[SimpleNamespace(manifest=SimpleNamespace(module_id='channel.custom'),provider=SimpleNamespace(normalize=lambda *a:None))]
    ChannelHostModule(registry).preflight_module({})
    assert called==[True]
