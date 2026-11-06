import pytest
from types import SimpleNamespace as NS
from staffdeck_harness.runtime.result_repair import ResultRepairState, ResultRepairRejected
from staffdeck_harness.runtime.structured_output import StructuredOutputError
from staffdeck_harness.sop.submission import STEP_RESULT_SCHEMA
from staffdeck_harness.contracts.invocation import ModuleResult


def state():
    value=ResultRepairState()
    value.capture('{"status":"completed","reply_fragment":"answer","next_step_id": done}')
    return value


def test_repair_budget_is_separate_and_retains_original_json():
    value=state()
    calls=[]
    def invoke(payload, attempt):
        calls.append(payload)
        return value.raw
    with pytest.raises(StructuredOutputError):
        value.repair(invoke,schema=STEP_RESULT_SCHEMA,allowed_next_steps={'done'},
            validate=lambda x:ModuleResult.ok(x),check_active=lambda:None,trace=lambda *a:None)
    assert len(calls)==2
    assert all(p['raw_submission']==value.raw for p in calls)


def test_sop_semantic_denial_is_not_retried_or_committed():
    value=state()
    calls=[]
    def invoke(payload, attempt):
        calls.append(attempt)
        return value.raw.replace(': done', ': "done"')
    with pytest.raises(ResultRepairRejected):
        value.repair(invoke,schema=STEP_RESULT_SCHEMA,allowed_next_steps={'done'},
            validate=lambda x:ModuleResult.fail('REQUIRED_SLOT_MISSING','missing fact'),
            check_active=lambda:None,trace=lambda *a:None)
    assert calls==[0]


def test_cancelled_repair_never_calls_model():
    from app.core.harness_agent import HarnessExecutionCancelled
    def check():raise HarnessExecutionCancelled('cancelled')
    with pytest.raises(HarnessExecutionCancelled):
        state().repair(lambda *a:pytest.fail('must not call'),schema=STEP_RESULT_SCHEMA,
            allowed_next_steps={'done'},validate=lambda x:ModuleResult.ok(x),check_active=check,trace=lambda *a:None)


def test_result_repair_phase_refuses_all_capabilities():
    from staffdeck_harness.bridge.phases import PhaseHost
    host=PhaseHost(model_config=NS(),phase='sop_result_repair')
    assert host.model_tool_names()==set()
    result,_=host.invoke_proxy('knowledge_search',{'query':'must not run'},None)
    assert not result.success and result.error['code']=='ACTIVATION_FENCED'


def test_legacy_raw_recovery_is_scoped_and_never_uses_an_obsolete_attempt():
    from datetime import timedelta
    from sqlmodel import SQLModel,Session,create_engine
    from app.db.models import HarnessRunRecord,AgentEvent,utc_now
    from staffdeck_harness.runtime.result_repair import recover_raw_submission
    engine=create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        run=HarnessRunRecord(id='run',tenant_id='tenant',session_id='session',task_id='task',
            task_frame_record_id='frame',agent_loop_id='loop',source_turn_id='turn',status='failed',
            task_requirement_json={'sop_context':{'step':{'node_id':'node'}}},
            result_json={'error':{'code':'SOP_RESULT_REPAIR_EXHAUSTED'}})
        db.add(run)
        db.add(AgentEvent(tenant_id='tenant',session_id='session',event_type='harness_action_created',
            payload_json={'harness_run_id':'run','control':'submit_step_result','arguments':state().raw}))
        db.commit()
        context=dict(tenant_id='tenant',session_id='session',task_id='task',loop_id='loop',step_id='node')
        assert recover_raw_submission(db,**context)==state().raw
        for key in context:
            assert recover_raw_submission(db,**{**context,key:'foreign'}) is None
        db.add(HarnessRunRecord(tenant_id='tenant',session_id='session',task_id='task',
            task_frame_record_id='frame',agent_loop_id='loop',source_turn_id='new',status='completed',
            created_at=utc_now()+timedelta(seconds=1)))
        db.commit()
        assert recover_raw_submission(db,**context) is None
