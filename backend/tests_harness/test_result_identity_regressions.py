"""Regression tests for resource identity, immutable replay and async envelopes."""
from dataclasses import replace
from types import SimpleNamespace
import json
import pytest

from sqlmodel import select
from app.agents.branching import ensure_private_resource_binding
from app.db.models import ExternalBusinessTask, ExternalBusinessTaskEvent
from app.tools.external_tasks import apply_task_event
from staffdeck_harness.composition.compiler import CompositionCompiler
from staffdeck_harness.contracts.hooks import HookDecision
from staffdeck_harness.contracts.invocation import ModuleResult
from tests_harness.test_capability_host_hardening import db as db, _tool, _staff, _cap, _ctx
from tests_harness.test_modular_boundary_regressions import configured_host


@pytest.mark.parametrize('status', ['completed', 'started', 'outcome_unknown'])
@pytest.mark.parametrize('identity', ['same', 'missing'])
def test_legacy_claims_never_resubmit_uncertain_writes(db, monkeypatch, status, identity):
    from app.db.models import HarnessInvocationRecord
    _, provider, host, inv = configured_host(db, monkeypatch)
    _, receipt = host.invoke(inv)
    row = db.get(HarnessInvocationRecord, receipt.ledger_id)
    row.logical_action_key = inv.legacy_side_effect_key()
    row.status = status
    if identity == 'missing':
        row.approval_json = {}
        row.arguments_json = {'_truncated': True}
    db.add(row)
    db.commit()
    result, replay = host.invoke(replace(inv, invocation_id='legacy-retry'))
    assert len(provider.calls) == 1
    if status == 'completed' and identity == 'same':
        assert result.success and replay.replayed_from
    else:
        assert result.error['code'] == 'OUTCOME_UNKNOWN'


def test_legacy_claim_for_other_tool_does_not_block_new_resource(db, monkeypatch):
    from app.db.models import HarnessInvocationRecord
    first_tool, provider, host, inv = configured_host(db, monkeypatch)
    inv = replace(inv, arguments={'order_id': 'x'})
    _, receipt = host.invoke(inv)
    row = db.get(HarnessInvocationRecord, receipt.ledger_id)
    row.logical_action_key = inv.legacy_side_effect_key()
    db.add(row)
    second_tool = _tool(db, 'b')
    ensure_private_resource_binding(db, 't1', 'a1', 'tool', second_tool.id)
    db.commit()
    host.slot.snapshot = CompositionCompiler(hooks=()).compile(_staff([_cap('tool', first_tool.id), _cap('tool', second_tool.id)]))
    result, receipt = host.invoke(replace(inv, invocation_id='other', binding_id=second_tool.id))
    assert result.success and not receipt.replayed_from and len(provider.calls) == 2


def test_replay_guard_denial_is_live_and_does_not_poison_cache(db, monkeypatch):
    policy = {'deny': False}
    seen = []
    def hook(point, inv, result):
        seen.append(point)
        if point == 'replay_tool' and policy['deny']:
            return HookDecision.deny('current output restriction')
        return HookDecision.passthrough()
    _, provider, host, inv = configured_host(db, monkeypatch, hooks=hook)
    assert host.invoke(inv)[0].success
    policy['deny'] = True
    assert host.invoke(replace(inv, invocation_id='denied'))[0].error['code'] == 'POST_TOOL_DENIED'
    policy['deny'] = False
    assert host.invoke(replace(inv, invocation_id='allowed'))[0].success
    assert seen.count('post_tool') == 1 and seen.count('pre_tool') == 3 and seen.count('replay_tool') == 2
    assert len(provider.calls) == 1


def test_replay_still_checks_current_resource_permissions(db, monkeypatch):
    tool, provider, host, inv = configured_host(db, monkeypatch)
    assert host.invoke(inv)[0].success
    tool.enabled = False
    db.add(tool)
    db.commit()
    assert host.invoke(replace(inv, invocation_id='revoked'))[0].error['code'] == 'RESOURCE_UNAVAILABLE'
    assert len(provider.calls) == 1


def test_replay_guard_cannot_transform_cached_result(db, monkeypatch):
    from staffdeck_harness.runtime.result_policy import check_replay
    result = ModuleResult.ok({'n': 1})
    def hook(*args):
        args[-1].data['n'] = 999
        return HookDecision.passthrough()
    assert check_replay(hook, None, result).data == {'n': 1}
    assert check_replay(lambda *a: HookDecision(kind='modify', replacement=ModuleResult.ok({})), None, result).error['code'] == 'REPLAY_POLICY_INVALID'


def test_replay_hook_is_a_registered_pipeline_point():
    from staffdeck_harness.composition.compiler import compile_hooks
    from staffdeck_harness.contracts.manifest import HookContribution
    from staffdeck_harness.contracts.hooks import HookContext
    from staffdeck_harness.interactions.pipeline_host import InteractionPipelineHost, PipelineState
    plan = compile_hooks([HookContribution(point='replay_tool', handler='guard')])
    pipeline = InteractionPipelineHost(plan, {'guard': lambda *a: HookDecision.deny('blocked')})
    context = HookContext('replay_tool', 't', 'a', 's', 'turn', 1, 'snap', {})
    assert pipeline.run('replay_tool', context, PipelineState(snapshot=None)).kind == 'deny'


def test_async_empty_approved_result_clears_older_partial_body(db, monkeypatch):
    from app.api.external_business_tasks import task_read
    task = ExternalBusinessTask(id='empty-final', tenant_id='t1', tool_id='tool_a',
        user_id='u1', callback_token_hash='audit-only', status='working',
        result_json={'old': 'partial'}, status_config_json={'_runtime': {'audit': True}})
    db.add(task)
    db.commit()
    monkeypatch.setattr('staffdeck_harness.runtime.external_tasks.project_external_result',
                        lambda *a: ModuleResult.ok(None))
    apply_task_event(db, task, event_id='final', event_type='result', status='completed',
                     data={'result': {'old': 'partial'}})
    public = task_read(task, db)
    assert public['result'] == {} and public['module_result']['data'] is None
    assert 'partial' not in json.dumps(public)


def test_legacy_identity_falls_back_only_to_exact_resource_argument(db, monkeypatch):
    from app.db.models import HarnessInvocationRecord
    _, provider, host, inv = configured_host(db, monkeypatch)
    _, receipt = host.invoke(inv)
    row = db.get(HarnessInvocationRecord, receipt.ledger_id)
    row.logical_action_key = inv.legacy_side_effect_key()
    row.approval_json = {}
    db.add(row)
    db.commit()
    result, receipt = host.invoke(replace(inv, invocation_id='old-argument'))
    assert result.success and receipt.replayed_from and len(provider.calls) == 1


def test_replayed_projection_is_not_transformed_again(db, monkeypatch):
    count = []
    def hook(point, inv, result):
        if point != 'post_tool':
            return HookDecision.passthrough()
        count.append(1)
        return HookDecision(kind='modify', replacement=ModuleResult.ok({'amount': result.data.get('amount', 0) + 1}))
    _, provider, host, inv = configured_host(db, monkeypatch, hooks=hook)
    first, _ = host.invoke(inv)
    second, receipt = host.invoke(replace(inv, invocation_id='again'))
    assert first.data['amount'] == second.data['amount'] == 1
    assert len(provider.calls) == 1 and len(count) == 1 and receipt.replayed_from


def test_different_bound_tools_do_not_collide(db, monkeypatch):
    first_tool, provider, host, inv = configured_host(db, monkeypatch)
    second_tool = _tool(db, 'tool_b')
    ensure_private_resource_binding(db, 't1', 'a1', 'tool', second_tool.id)
    db.commit()
    host.slot.snapshot = CompositionCompiler(hooks=()).compile(_staff([_cap('tool', first_tool.id), _cap('tool', second_tool.id)]))
    calls = []
    def invoke(h, i):
        calls.append(i.binding_id)
        return ModuleResult.ok({'actual_tool': i.binding_id})
    monkeypatch.setattr(provider, 'invoke', invoke)
    a = replace(inv, arguments={'order_id': 'same'}, binding_id=first_tool.id)
    b = replace(a, invocation_id='second-tool', binding_id=second_tool.id)
    assert a.side_effect_key() != b.side_effect_key()
    first, _ = host.invoke(a)
    second, receipt = host.invoke(b)
    assert first.success and second.success
    assert calls == [first_tool.id, second_tool.id] and not receipt.replayed_from
    assert second.data['actual_tool'] == second_tool.id


def test_async_preserves_approved_metadata(db, monkeypatch):
    task = ExternalBusinessTask(id='async-audit', tenant_id='t1', tool_id='tool_a', user_id='u1', callback_token_hash='audit-only',
                               status='working', status_config_json={'_runtime': {'audit': True}})
    db.add(task)
    db.commit()
    final = ModuleResult.ok({'ok': True}, citations=({'id': 'c1', 'excerpt': 'audit'},),
                            artifacts=({'path': 'report.txt'},), extensions={'evidence': {'id': 'e1'}})
    monkeypatch.setattr('staffdeck_harness.runtime.external_tasks.project_external_result', lambda *a: final)
    apply_task_event(db, task, event_id='final', event_type='result', status='completed', data={'result': {'ok': True}})
    db.refresh(task)
    event = db.exec(select(ExternalBusinessTaskEvent).where(ExternalBusinessTaskEvent.task_id == task.id)).one()
    saved = json.dumps({'task': task.result_json, 'event': event.data_json})
    assert 'report.txt' in saved and 'citations' in saved and 'evidence' in saved


def test_tool_proxy_keeps_resource_identity_with_selected_idempotency_fields(db, monkeypatch):
    first_tool, provider, host, inv = configured_host(db, monkeypatch)
    first_tool.config_json = {'idempotency': {'enabled': True, 'key_fields': ['order_id']}}
    second_tool = _tool(db, 'tool_b', idempotency={'enabled': True, 'key_fields': ['order_id']})
    ensure_private_resource_binding(db, 't1', 'a1', 'tool', second_tool.id)
    db.add(first_tool)
    db.commit()
    host.slot.snapshot = CompositionCompiler(hooks=()).compile(_staff([_cap('tool', first_tool.id), _cap('tool', second_tool.id)]))
    calls = []
    def invoke(h, i):
        calls.append(i.binding_id)
        return ModuleResult.ok({'actual_tool': i.binding_id})
    monkeypatch.setattr(provider, 'invoke', invoke)
    first, _ = host.invoke_proxy('tool_invoke', {'tool_id': first_tool.id, 'arguments': {'order_id': 'same'}}, _ctx(trace_id='first'))
    second, receipt = host.invoke_proxy('tool_invoke', {'tool_id': second_tool.id, 'arguments': {'order_id': 'same'}}, _ctx(trace_id='second'))
    assert first.success and second.success
    assert calls == [first_tool.id, second_tool.id] and second.data['actual_tool'] == second_tool.id and not receipt.replayed_from
