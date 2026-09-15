import ast
import json
import threading
from pathlib import Path
from types import SimpleNamespace as NS
import pytest

from staffdeck_harness.runtime.structured_output import generate_structured, validate_schema
from staffdeck_harness.sop.submission import STEP_RESULT_SCHEMA
from staffdeck_harness.bridge.control import ExecutionHost
from app.core.task_request_compiler import TaskRequirement


def test_runtime_repairs_bare_step_identifier_once_without_business_calls():
    calls=[]
    bad='{"status":"completed","reply_fragment":"saved result","next_step_id": final_response}'
    def model(payload, attempt):
        calls.append(payload)
        return bad if attempt==0 else bad.replace(': final_response', ': "final_response"')
    result=generate_structured(model, {'evidence':['already executed']},
        lambda value:validate_schema(value,STEP_RESULT_SCHEMA),phase='sop_result')
    assert result['next_step_id']=='final_response' and len(calls)==2
    assert calls[1]['_schema_repair']['previous_output']==bad
    assert calls[1]['evidence']==['already executed']


def test_transport_timeout_is_not_a_schema_retry():
    calls=[]
    def model(payload,attempt):
        calls.append(attempt)
        raise TimeoutError('upstream timeout')
    with pytest.raises(TimeoutError):
        generate_structured(model,{},lambda x:x,phase='plan')
    assert calls==[0]


def test_sop_submission_errors_do_not_consume_capability_recovery():
    slot=NS(closed=False,allowed_next_steps={'final_response'},finish=None)
    def forbid(*args): raise AssertionError('must not dispatch a business capability')
    capabilities=NS(_invoke_lock=threading.RLock(),slot=slot,registry=None,
        fence=NS(check=lambda slot:None),results=[],citations=[],evidence=[],
        _emit=lambda *a:None,invoke_proxy=forbid)
    host=ExecutionHost(capabilities,TaskRequirement(task_frame_id='sop',kind='sop',goal='answer'))
    for _ in range(3):
        result,_=host.invoke_proxy('submit_step_result',{},None)
        assert not result.success
    assert host.recovery.counts=={} and host.recovery_blocked is None
    assert host.control_blocked['code']=='SOP_RESULT_REPAIR_EXHAUSTED'
    assert slot.finish is None and slot.closed is False


def test_domain_boundaries_have_no_model_transport_or_bridge_imports():
    root=Path(__file__).parents[1]
    for path in [root/'app/channels/service_autoroute.py',root/'src/staffdeck_harness/routing/intent.py',
                 root/'src/staffdeck_harness/sop/submission.py']:
        tree=ast.parse(path.read_text())
        imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        assert not any(m.startswith(('app.llm','staffdeck_harness.bridge')) for m in imports), path
        assert 'generate_text' not in path.read_text()


def test_inference_resolves_model_through_selected_staff_source(monkeypatch):
    from staffdeck_harness.runtime.inference import resolve_model
    calls=[]
    registry=object()
    context=NS(tenant_id='tenant',staff_id='staff')
    monkeypatch.setattr('staffdeck_harness.runtime.staff_directory.directory_context',lambda *a:context)
    monkeypatch.setattr('staffdeck_harness.security.profile.get_profile',lambda *a:object())
    source=NS(model=lambda ctx:calls.append(ctx) or 'selected-model')
    monkeypatch.setattr('staffdeck_harness.composition.sources.authorize_staff',lambda *a:(None,source,None))
    assert resolve_model(NS(info={'staffdeck_registry':registry}),'tenant','staff')=='selected-model'
    assert calls==[context]


def test_bridge_uses_selected_sop_validator_and_cannot_commit_a_denial():
    from staffdeck_harness.bridge.control import StepCompletionPort
    from staffdeck_harness.contracts.invocation import ModuleResult
    calls=[]
    def submit(arguments, **context):
        calls.append(context)
        return ModuleResult.fail('CUSTOM_SOP_DENIED','domain rejected')
    provider=NS(submission_validator=lambda requirement:NS(submit=submit))
    slot=NS(finish=None,closed=False,allowed_next_steps={'finish'})
    host=NS(registry=NS(provider=lambda slot:NS(provider=provider)),slot=slot,
        fence=NS(check=lambda slot:None),results=[],citations=[],evidence=[],_emit=lambda *a:None)
    result=StepCompletionPort(host,TaskRequirement(task_frame_id='s',kind='sop',goal='g')).submit(
        {'status':'completed','reply_fragment':'done'})
    assert result.error['code']=='CUSTOM_SOP_DENIED' and calls
    assert slot.finish is None and not slot.closed
