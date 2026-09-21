"""Real Host/ledger -> SOP next-step regressions; no model or business network."""
import json
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.core.task_request_compiler import TaskExecutionResult, TaskRequirement
from app.core.turn_coordinator import _prior_result
from app.db.models import HarnessInvocationRecord
from staffdeck_harness.bridge.task_agent import _step_prompt
from staffdeck_harness.contracts.hooks import HookDecision
from staffdeck_harness.contracts.invocation import ModuleResult, Receipt, normalize_result
from staffdeck_harness.contracts.json_values import JsonValueError, json_safe
from staffdeck_harness.interactions.pipeline_host import PipelineState
from staffdeck_harness.runtime.external_tasks import project_external_result
from staffdeck_harness.runtime.result_policy import project_result
from tests_harness.test_capability_host_hardening import db as db
from tests_harness.test_modular_boundary_regressions import configured_host

STAMP = datetime(2026, 9, 20, 12, 34, 56, tzinfo=timezone.utc)


def prompt_for(results, **kw):
    req = TaskRequirement(task_frame_id='step2', kind='sop', goal='second step',
                          prior_task_results=results, **kw)
    return _step_prompt(req, PipelineState(snapshot=None), [], '')


def test_explicit_types_and_no_mutation():
    original = {'nested': [STAMP, date(2026, 9, 20), Decimal('12.3400'), UUID(int=1)],
                'tuple': (1, True, None)}
    safe = json_safe(original)
    assert safe['nested'] == [STAMP.isoformat(), '2026-09-20', '12.3400', str(UUID(int=1))]
    assert safe['tuple'] == [1, True, None]
    assert original['nested'][0] is STAMP
    json.dumps(safe, allow_nan=False)


@pytest.mark.parametrize('bad', [object(), b'private-bytes', {1, 2}, float('nan'),
                                float('inf'), Decimal('NaN'), {1: 'invalid-key'}])
def test_invalid_values_have_path_and_never_stringify(bad):
    with pytest.raises(JsonValueError) as caught:
        json_safe({'items': [bad]})
    assert '$.items[0]' in caught.value.message
    assert 'private-bytes' not in caught.value.message


def test_cycles_rejected_but_shared_objects_allowed():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(JsonValueError, match='cyclic'):
        json_safe(cycle)
    shared = {'time': STAMP}
    assert json_safe([shared, shared]) == [{'time': STAMP.isoformat()}] * 2


def test_receipt_and_all_module_fields_normalized():
    receipt = Receipt('i', 'completed', 'd', started_at=STAMP, finished_at=STAMP,
                      error={'when': STAMP})
    assert receipt.to_json()['error']['when'] == STAMP.isoformat()
    original = ModuleResult(False, {'when': STAMP}, {'when': STAMP},
                            ({'when': STAMP},), ({'when': STAMP},), {'when': STAMP})
    result = normalize_result(original)
    json.dumps(asdict(result), allow_nan=False)
    assert result.extensions['when'] == STAMP.isoformat()
    assert original.data['when'] is STAMP


@pytest.mark.parametrize('replacement', [False, True])
def test_hooks_receive_safe_data_and_final_projection_is_rechecked(replacement):
    def hooks(point, inv, result):
        assert result.data['when'] == STAMP.isoformat()
        if replacement:
            return HookDecision(kind='modify', replacement=ModuleResult.ok({'after': STAMP}))
        result.data['after'] = STAMP
        return HookDecision.passthrough()
    result = project_result(hooks, None, ModuleResult.ok({'when': STAMP}))
    assert result.data['after'] == STAMP.isoformat()
    json.dumps(asdict(result))


@pytest.mark.parametrize('origin', ['provider', 'hook'])
def test_invalid_output_does_not_repeat_executed_write(db, monkeypatch, origin):
    def hooks(point, inv, result):
        if origin == 'hook' and point == 'post_tool':
            return HookDecision(kind='modify', replacement=ModuleResult.ok({'bad': object()}))
        return HookDecision.passthrough()
    _, provider, host, inv = configured_host(db, monkeypatch, hooks=hooks)
    sent = []
    def invoke(*args):
        sent.append(1)
        return ModuleResult.ok({'bad': object()} if origin == 'provider' else {'ok': True})
    monkeypatch.setattr(provider, 'invoke', invoke)
    first, receipt = host.invoke(inv)
    second, replay = host.invoke(replace(inv, invocation_id='i2'))
    assert first.error['code'] == second.error['code'] == 'RESULT_NOT_JSON_SAFE'
    assert first.error['retryable'] is False and first.error['business_replay_allowed'] is False
    assert receipt.status == 'completed' and replay.replayed_from
    assert sent == [1]
    row = db.get(HarnessInvocationRecord, receipt.ledger_id)
    assert row.logical_action_key and row.status == 'completed'
    json.dumps(row.result_json)
    json.dumps(row.response_cache_json)


def test_host_persisted_receipt_to_next_sop_step(db, monkeypatch):
    def hook(point, inv, result):
        if point == 'post_tool':
            assert result.data['when'] == STAMP.isoformat()
            return HookDecision(kind='modify', replacement=ModuleResult.ok({'when': STAMP, 'checked': True}))
        return HookDecision.passthrough()
    _, provider, host, inv = configured_host(db, monkeypatch, hooks=hook)
    monkeypatch.setattr(provider, 'invoke', lambda *a: ModuleResult.ok({'when': STAMP}))
    result, receipt = host.invoke(inv)
    row = db.get(HarnessInvocationRecord, receipt.ledger_id)
    db.refresh(row)
    assert row.response_cache_json['data']['when'] == STAMP.isoformat()
    assert result.data['checked']
    step1 = TaskExecutionResult(task_frame_id='step1', status='completed', capability_results=host.results)
    prior = _prior_result(step1)
    assert STAMP.isoformat() in prompt_for([prior])
    assert STAMP.isoformat() in prompt_for([json.loads(json.dumps(prior))])


def test_prompt_defense_for_legacy_results_slots_and_transitions():
    raw_receipt = asdict(Receipt('i', 'completed', 'd', started_at=STAMP, finished_at=STAMP))
    prior = [{'capability_results': [{'receipt': raw_receipt, 'data': {'when': STAMP}}]}]
    text = prompt_for(prior, known_slots={'when': STAMP}, allowed_transitions=[{'when': STAMP}])
    assert text.count(STAMP.isoformat()) == 5
    assert isinstance(raw_receipt['started_at'], datetime) and raw_receipt['started_at'] == STAMP
    with pytest.raises(JsonValueError, match=r'\$\.task.known_slots.bad'):
        prompt_for([], known_slots={'bad': object()})


@pytest.mark.parametrize('active', [True, False])
def test_async_projection_uses_shared_boundary(db, monkeypatch, active):
    task = SimpleNamespace(id='async1', status_config_json={})
    if active:
        _, _, host, inv = configured_host(db, monkeypatch)
        db.info['staffdeck_external_activation'] = (task.id, host, inv)
    else:
        monkeypatch.setattr('staffdeck_harness.modules.registry.peek_registry', lambda: None)
    result = project_external_result(db, task, ModuleResult.ok({'when': STAMP}))
    assert result.data['when'] == STAMP.isoformat()
    assert STAMP.isoformat() in prompt_for([{'capability_results': [asdict(result)]}])


@pytest.mark.parametrize('delivery', ['worker', 'callback'])
@pytest.mark.parametrize('invalid', [False, True])
def test_async_completion_persistence_and_sop_resume(db, monkeypatch, delivery, invalid):
    from sqlmodel import select
    from app.agents.branching import ensure_private_resource_binding
    from app.db.models import ChatSession, ExternalBusinessTask, ExternalBusinessTaskEvent, HarnessTaskFrameRecord
    from app.tools.external_tasks import apply_task_event, poll_due_external_tasks
    from app.tools.tool_executor import ToolResult
    from staffdeck_harness.composition.compiler import CompositionCompiler
    from staffdeck_harness.contracts.invocation import ModuleInvocation
    from staffdeck_harness.contracts.manifest import HookContribution
    from staffdeck_harness.modules.builtin import LocalCapabilityProvider
    from tests_harness.test_capability_host_hardening import _tool, _install_registry, _host, _staff, _cap, _ctx

    tool = _tool(db, 'json-async')
    tool.config_json = {'execution': {'execution_mode': 'detached', 'timeout_seconds': 10}}
    ensure_private_resource_binding(db, 't1', 'a1', 'tool', tool.id)
    db.add_all([tool, ChatSession(id='s1', tenant_id='t1', agent_id='a1', user_id='u1')])
    db.commit()
    registry = _install_registry(monkeypatch, LocalCapabilityProvider())
    observed = []
    def policy(ctx, state):
        if ctx.payload['data'].get('detached'):
            return HookDecision.passthrough()
        observed.append(ctx.payload['data'])
        assert ctx.payload['data']['when'] == STAMP.isoformat()
        return HookDecision(kind='modify', replacement=ModuleResult.ok(
            {'after': object() if invalid else STAMP}))
    monkeypatch.setattr(registry, 'hooks', lambda *a, **kw: (
        HookContribution(point='post_tool', handler='test.json', critical=True),))
    monkeypatch.setattr(registry, 'hook_handlers', lambda *a, **kw: {'test.json': policy})
    host = _host(db, CompositionCompiler(hooks=()).compile(_staff([_cap('tool', tool.id)])))
    monkeypatch.setattr('staffdeck_harness.security.profile.get_profile', lambda: host.guard.profile)
    result, _ = host.invoke(ModuleInvocation('async-json', 'tool', 'tool.invoke/v1', {},
        _ctx(), binding_id=tool.id, metadata={'proxy_name': 'tool_invoke'}))
    assert result.success
    task = db.get(ExternalBusinessTask, result.data['task_id'])
    frame = HarnessTaskFrameRecord(tenant_id='t1', session_id='s1', source_turn_id='turn1',
        task_id='tf1', kind='sop', status='waiting_external_task',
        result_json={'structured_result': {'task_id': task.id}})
    db.add(frame)
    db.commit()
    if delivery == 'worker':
        monkeypatch.setattr('app.tools.tool_executor.ToolExecutor.execute_sync_http',
            lambda *a: ToolResult(tool_name=tool.name, success=True, data={'when': STAMP}))
        poll_due_external_tasks(db)
    else:
        apply_task_event(db, task, event_id='json-final', event_type='result', status='completed',
                         data={'result': {'when': STAMP}})
    db.refresh(task)
    db.refresh(frame)
    assert observed == [{'when': STAMP.isoformat()}]
    json.dumps([event.data_json for event in db.exec(select(ExternalBusinessTaskEvent)).all()], allow_nan=False)
    json.dumps(frame.result_json, allow_nan=False)
    if invalid:
        assert task.status == 'tracking_blocked'
        assert task.error_json['code'] == 'RESULT_NOT_JSON_SAFE'
        assert not task.error_json['business_replay_allowed']
        assert frame.status == 'waiting_external_task'
    else:
        assert task.status == 'completed'
        assert frame.status == 'ready_to_resume'
        assert frame.slots_json == {'after': STAMP.isoformat()}
        assert STAMP.isoformat() in prompt_for([frame.result_json], known_slots=frame.slots_json)
