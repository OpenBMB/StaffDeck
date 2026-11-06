from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from app.db.models import ExternalBusinessTask, utc_now
from app.tools.external_tasks import _poll_task
from staffdeck_harness.contracts.errors import ModuleSdkError


@pytest.mark.parametrize('code,blocked', [('RESOURCE_UNAVAILABLE',True), ('PERMISSION_DENIED',True),
    ('CAPABILITY_SNAPSHOT_CHANGED',True), ('AUTHORIZATION_UNAVAILABLE',False), ('ENGINE_UNAVAILABLE',False)])
def test_module_error_classification_preserves_code_and_does_not_repeat_business(monkeypatch, code, blocked):
    @contextmanager
    def unavailable(db, task):
        raise ModuleSdkError('test reason',code=code)
        yield
    monkeypatch.setattr('staffdeck_harness.runtime.external_tasks.resolve_task_tool',unavailable)
    monkeypatch.setattr('app.tools.external_tasks.httpx.get',lambda *a, **kw:pytest.fail('No request after authorization failure'))
    engine = create_engine('sqlite://',poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        task = ExternalBusinessTask(tenant_id='t',user_id='u',tool_id='tool',callback_token_hash='hash',
            status='accepted',external_task_id='0',status_url='http://unused.invalid/{taskId}',
            next_poll_at=utc_now()-timedelta(seconds=1),lease_owner='owner')
        db.add(task)
        db.commit()
        _poll_task(db,task,owner='owner')
        db.refresh(task)
        assert task.error_json['code'] == code
        assert task.error_json['retryable'] == (not blocked)
        assert task.error_json['business_replay_allowed'] is False
        assert (task.status == 'tracking_blocked') == blocked
        assert (task.next_poll_at is None) == blocked


def test_legacy_callback_cannot_skip_selected_result_policy(monkeypatch):
    import json
    from app.tools.external_tasks import apply_task_event
    from app.api.external_business_tasks import task_read
    monkeypatch.setattr('staffdeck_harness.modules.registry._active', SimpleNamespace())
    engine = create_engine('sqlite://',poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        task=ExternalBusinessTask(tenant_id='t',user_id='u',tool_id='tool',callback_token_hash='hash',status='accepted')
        db.add(task)
        db.commit()
        apply_task_event(db,task,event_id='callback',event_type='result',status='completed',data={'result':{'private':'SECRET'}})
        assert task.status=='tracking_blocked'
        assert task.error_json['code']=='ASYNC_CONTEXT_MISSING'
        assert 'SECRET' not in json.dumps(task_read(task,db))


def test_stale_poll_cannot_regress_callback_terminal_state(monkeypatch):
    from app.tools.external_tasks import apply_task_event
    monkeypatch.setattr('staffdeck_harness.modules.registry._active',None)
    engine=create_engine('sqlite://',poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as first, Session(engine) as stale:
        task=ExternalBusinessTask(tenant_id='t',user_id='u',tool_id='tool',callback_token_hash='h',status='accepted')
        first.add(task)
        first.commit()
        old=stale.get(ExternalBusinessTask,task.id)
        apply_task_event(first,task,event_id='done',event_type='callback',status='completed',data={'result':{'safe':True}})
        apply_task_event(stale,old,event_id='progress',event_type='poll',status='working',data={'result':{'wrong':True}})
        first.refresh(task)
        assert task.status=='completed' and task.result_json=={'safe':True}


def test_plain_progress_cannot_lift_tracking_block(monkeypatch):
    from app.tools.external_tasks import apply_task_event
    monkeypatch.setattr('staffdeck_harness.modules.registry._active',None)
    engine=create_engine('sqlite://',poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        task=ExternalBusinessTask(tenant_id='t',user_id='u',tool_id='tool',callback_token_hash='h',
            status='tracking_blocked',status_config_json={'_result_blocked':True},error_json={'code':'POST_TOOL_DENIED'})
        db.add(task)
        db.commit()
        apply_task_event(db,task,event_id='progress',event_type='callback',status='working',data={})
        assert task.status=='tracking_blocked' and task.next_poll_at is None
        assert task.error_json['code']=='POST_TOOL_DENIED'


def test_temporary_policy_reconstruction_failure_does_not_require_provider_callback(monkeypatch):
    from app.tools.external_tasks import apply_task_event
    def unavailable(*args):
        raise ModuleSdkError('authorization offline',code='AUTHORIZATION_UNAVAILABLE')
    monkeypatch.setattr('staffdeck_harness.runtime.external_tasks.project_external_result',unavailable)
    engine=create_engine('sqlite://',poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        task=ExternalBusinessTask(tenant_id='t',user_id='u',tool_id='tool',callback_token_hash='h',
            status='accepted',status_config_json={'_runtime':{'actor':{}}})
        db.add(task)
        db.commit()
        apply_task_event(db,task,event_id='done',event_type='poll',status='completed',data={'result':{'private':'SECRET'}})
        assert task.status=='tracking_blocked'
        assert task.error_json['code']=='AUTHORIZATION_UNAVAILABLE'
        assert task.status_config_json['_result_blocked'] is False
        assert task.result_json=={}


def test_explicit_resume_only_reauthorizes_tracking_without_submit(monkeypatch):
    from app.db.models import User
    from app.api.external_business_tasks import resume_external_task_tracking
    calls=[]
    @contextmanager
    def authorized(db, task):
        calls.append(task.id)
        yield None
    monkeypatch.setattr('staffdeck_harness.runtime.external_tasks.resolve_task_tool',authorized)
    monkeypatch.setattr('app.tools.external_tasks.httpx.post',lambda *a, **kw:pytest.fail('No new business POST'))
    engine=create_engine('sqlite://',poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user=User(id='u',tenant_id='t',username='u',password_hash='x')
        task=ExternalBusinessTask(tenant_id='t',user_id='u',tool_id='tool',callback_token_hash='h',
            status='tracking_blocked',external_task_id='0',status_url='http://unused.invalid/0',
            status_config_json={'_result_blocked':False})
        db.add_all([user,task])
        db.commit()
        result=resume_external_task_tracking(task.id,'t',db,user)
        assert calls==[task.id]
        assert result['status']=='accepted' and task.next_poll_at is not None
