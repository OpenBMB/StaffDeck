from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.scheduled_tasks import list_enterprise_scheduled_task_runs_for_agent
from app.db.models import ScheduledTask, ScheduledTaskRun, Tenant, User, utc_now


def test_run_directory_uses_separate_definition_and_history_binds():
    control = create_engine('sqlite://', poolclass=StaticPool)
    runtime = create_engine('sqlite://', poolclass=StaticPool)
    SQLModel.metadata.create_all(control, tables=[Tenant.__table__, ScheduledTask.__table__])
    SQLModel.metadata.create_all(runtime, tables=[ScheduledTaskRun.__table__])
    user = User(id='u', tenant_id='t', username='u', password_hash='x', role='member')
    with Session(binds={Tenant: control, ScheduledTask: control, ScheduledTaskRun: runtime}) as db:
        db.add(Tenant(id='t', name='Tenant'))
        db.add(ScheduledTask(id='task', tenant_id='t', agent_id='a', created_by_user_id='u',
                             title='Existing definition', prompt='test', schedule_type='daily'))
        for identifier, tenant, actor, task_id in (
            ('normal', 't', 'u', 'task'), ('orphan', 't', 'u', 'deleted-task'),
            ('other-user', 't', 'someone', 'task'), ('other-tenant', 'elsewhere', 'u', 'task'),
        ):
            db.add(ScheduledTaskRun(id=identifier, tenant_id=tenant, user_id=actor, agent_id='a',
                                    scheduled_task_id=task_id, scheduled_for=utc_now(), status='completed'))
        db.commit()
        rows = list_enterprise_scheduled_task_runs_for_agent('t', 'a', None, 100, user, db)
        by_id = {row.id: row for row in rows}
        assert set(by_id) == {'normal', 'orphan'}
        assert by_id['normal'].task_title == 'Existing definition'
        assert by_id['orphan'].task_title is None
        assert len(list_enterprise_scheduled_task_runs_for_agent('t', 'a', None, 1, user, db)) == 1
